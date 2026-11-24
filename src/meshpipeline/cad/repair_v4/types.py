# Responsibility: Shared plain-data types of the v4 repair engine that more than one module names.
# Owns: the `Site` dataclass - one located place where a mesher or checker objects to the part.
# Boundaries: data only. Site DETECTION and rendering live elsewhere (Task 17); nothing here
#             touches a kernel.
"""Plain-data types shared across the v4 repair modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: A stable, workspace-scoped name for one B-rep entity, e.g. "F41", "E7", "B2".
StableId = str

SiteKind = Literal[
    "proximity", "self_overlap", "intersection", "open", "overlap", "invalid", "sliver",
    "periodic", "pole", "timeout", "unlocated",
    # lane C7: layer 1's `unbounded_geometry` (an infinite surface) - the site `trim_unbounded` answers
    "unbounded",
]


@dataclass
class Site:
    id: str
    kind: SiteKind
    entities: list[StableId]
    xyz: list[tuple]
    meshers_failing: list[str]
    dist_over_h: float | None
    graph: dict
    view_png: bytes | None
