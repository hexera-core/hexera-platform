# Responsibility: Declare the surface a visual review presents to the model.
# Boundaries: a typed declaration; the renderer satisfies it.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class OpeningContext:

    nav_context: dict
    patch_colour_legend: str
    initial_screenshot_b64: str | None
    has_geometry: bool
    patch_names: list[str]
    # ADDITIVE typed target inventory. Engine-neutral: (kind, stable id, label). Production
    # coverage still uses `patch_names` until Commit C; this is how an engine whose targets are
    # NOT patches (Gmsh GROUPs, VMTK openings/branches) exposes them without faking `patch_names`.
    inspection_targets: tuple = ()   # tuple[InspectionTarget, ...] - no engine policy
    # RENDERER-DERIVED per-patch framing (tuple[PatchView, ...]). An opening fact of the same
    # kind as the legend: computed from the geometry the renderer loaded, never from the
    # manifest, so nothing upstream of the review chooses where the reviewer looks.
    patch_views: tuple = ()


@runtime_checkable
class ReviewerVisualSurface(Protocol):

    async def initial_context(self) -> OpeningContext:
        ...

    async def execute_viewer_tool(self, fn: str, args: dict) -> Any:
        ...
