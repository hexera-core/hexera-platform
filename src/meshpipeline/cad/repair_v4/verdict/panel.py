# Responsibility: The panel contract only - PanelMember, MemberResult, Located.
# Boundaries: STAND-IN for Task 3's verdict/panel.py, committed so Task 4 builds on its own. On merge
#             TAKE TASK 3's FILE; it is a superset (adds run_panel) with the same three types.
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from meshpipeline.cad.repair_v4.worker_pool import WorkerPool


@dataclass(frozen=True)
class Located:
    entity: str | None
    xyz: tuple[float, float, float] | None
    mesher_tag: str


@dataclass(frozen=True)
class MemberResult:
    name: str
    status: Literal["pass", "fail", "timeout", "crashed"]
    error_kind: str | None
    message: str
    located: list[Located] = field(default_factory=list)
    min_sicn: float | None = None
    elements: int = 0
    seconds: float = 0.0


class PanelMember(Protocol):
    name: str

    def run(self, pool: WorkerPool, step_path: str, *, timeout_s: float) -> MemberResult: ...
