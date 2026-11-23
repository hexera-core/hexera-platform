# Responsibility: The shared shapes of layer 2 of the CRS v4 checker - what one mesher on the panel
#                 reports - and the runner that puts a file in front of every member.
# Owns: Located, MemberResult, the PanelMember protocol and `run_panel`.
# Boundaries: no gmsh, no OCCT, no process handling. A member talks to the WorkerPool; which meshers
#             exist is `gmsh_members` (Task 4 adds Netgen). It judges nothing across members.
"""Layer 2 (meshable on a mesher panel). "Clean" has to hold in whatever tool the customer uses, so a
file is put in front of several unlike meshers and each answers for itself.

One bad mesher must not take down the panel: a member that segfaults or hangs is recorded `crashed`
or `timeout` (the pool kills and replaces its worker) and the members after it still run. An
INFRASTRUCTURE failure - a worker that cannot start, a kernel function that errors for a reason
that is not the file's - is not a mesher finding and raises `CheckerUnavailable`, as layer 1 does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

#: `MemberResult.error_kind` of a `timeout`/`crashed` result that came from the pool, not the mesher
#: (no worker could be had, or it was dead before the call): not a verdict on the file. Must equal
#: `assemble.INFRASTRUCTURE_KIND`, which is what the verdict reads.
INFRASTRUCTURE_KIND = "infrastructure"


@dataclass(frozen=True)
class Located:
    """Where a mesher says it went wrong. `entity` is the face's fingerprint (`identity.fingerprint_of`,
    from the surface's bounding box in mm) or None; `xyz` a point in mm or None; `mesher_tag` the
    mesher's own name for it (a gmsh surface tag, say), which means nothing outside that run."""

    entity: str | None
    xyz: tuple[float, float, float] | None
    mesher_tag: str


@dataclass(frozen=True)
class MemberResult:
    """One mesher's answer. `pass` means a valid tet mesh with no inverted element and
    `min_sicn >= 0.01`; `fail` names why in `error_kind`; `timeout` and `crashed` are the mesher
    hanging or dying on this file (a finding about the file, unlike an infrastructure failure)."""

    name: str
    status: Literal["pass", "fail", "timeout", "crashed"]
    error_kind: str | None
    message: str
    located: list[Located]
    min_sicn: float | None
    elements: int
    seconds: float


class PanelMember(Protocol):
    name: str
    #: Everything that decides this member's answer, JSON-able and never None: the verdict cache keys
    #: on its hash, so a changed option must change it.
    config: dict

    def run(self, pool: WorkerPool, step_path: str, *, timeout_s: float) -> MemberResult: ...


def run_panel(
    pool: WorkerPool, step_path: str, members: Sequence[PanelMember], *, timeout_s: float
) -> list[MemberResult]:
    """Run every member on `step_path`, in order, one result each. A member that times out or
    crashes is a result like any other; the next member runs on a replaced worker."""
    return [member.run(pool, step_path, timeout_s=timeout_s) for member in members]
