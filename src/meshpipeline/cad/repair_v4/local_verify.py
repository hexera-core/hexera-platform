# Responsibility: search_seq's LOCAL verification (lane C17): after one repair of one defect, is that defect gone on
#                 the entities the repair touched, with no new hard code there - without a full 4-mesher check.
# Owns: Defect, PairResult, LocalResult, `touched_bodies`, `relocate`, `verify_local`.
# Boundaries: no OCCT here; every kernel call goes through the WorkerPool handed in. It decides nothing about the
#             search: the arm (lane B) orders defects, picks repairs, keeps or reverts; this module only measures.
"""Local verification for the sequential defect-repair arm (`search_seq`, seq-arm-design.md).

The arm repairs ONE defect at a time and asks `verify_local` whether that repair worked, on the touched solids only:

- a VALIDITY defect (a layer-1 hard code): is this defect's code gone on the touched solids, and is there no hard
  code on them that the same solids did not already have before the repair;
- a MESHER defect (a panel member's failure): mesh the touched solid(s) alone with the member that failed (the
  doomed cap applies) and ask whether that member now passes.

A local pass is not a whole-part pass (body interactions, shared facets); the end-of-round full check is what
catches that. `verify_local` never claims clean.

Entity tracking: ops rename faces. `relocate` carries a defect's entities from the state it was diagnosed on to
the current state, through the ops' own history (`Diff.modified` / `generated` / `removed`) where it exists, else
by the defect's geometric location (its points / box). A defect that cannot be located any more is re-checked
locally by the arm: gone -> `resolved_by_earlier_repair`.

body_overlap on a multi-body part (owner note 1): each CONNECTED overlap cluster is ONE defect, repaired PAIR BY
PAIR. `verify_local(..., pair=(a, b))` checks that one pair (touched = the pair's two bodies + their neighbours)
and returns per-pair results for every pair of the cluster it measured, so one bad pair is skipped and recorded
without blocking the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from meshpipeline.cad.repair_v4.types import StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Workspace

#: Box = (xmin, ymin, zmin, xmax, ymax, zmax), mm.
Box = tuple[float, float, float, float, float, float]

#: the layer-1 hard codes a defect can carry (verdict.valid.HARD_CODES), as the arm sees them
VALIDITY = "validity"
MESHER = "mesher"


@dataclass(frozen=True)
class Defect:
    """One defect the arm repairs, as diagnosed on some state. Built by the arm from a `Site` (lane B).

    `code` is the layer-1 hard code (`body_overlap`, `self_intersection`, `open_shell`, ...) for a validity defect,
    or the failing member's `error_kind` (`overlapping_facets`, `low_quality`, ...) for a mesher defect.
    `entities` are stable ids on the state it was diagnosed on: bodies (B..) and/or faces (F..). For a body_overlap
    CLUSTER, `entities` are the cluster's bodies and `pairs` its overlapping pairs.
    """

    id: str  # the site id (stable across rounds through SiteRegistry); used only for the trace
    kind: Literal["validity", "mesher"]
    code: str
    entities: tuple[StableId, ...]
    member: str | None = None  # the panel member that failed, for a mesher defect
    xyz: tuple[tuple[float, float, float], ...] = ()  # located points (mesher nodes, finding points)
    box: Box | None = None  # the defect's extent, for geometric re-location
    pairs: tuple[tuple[StableId, StableId], ...] = ()  # body_overlap cluster: the overlapping body pairs
    measurement: dict = field(default_factory=dict)  # clash depth, gap, sliver width, ... (for the trace)


@dataclass(frozen=True)
class PairResult:
    """One body pair of an overlap cluster, after a repair aimed at one of them."""

    pair: tuple[StableId, StableId]  # stable ids on ws_after
    gone: bool  # no body_overlap between these two any more
    detail: dict = field(default_factory=dict)  # overlap volume / thickness when still there


@dataclass(frozen=True)
class LocalResult:
    """What `verify_local` measured. `gone and not new_hard` is a local pass; it is never a clean claim."""

    gone: bool  # this defect's code is no longer on the touched entities (mesher: the member now passes there)
    new_hard: list[dict]  # hard findings on the touched solids the same solids did not have before: {code, entities}
    detail: dict  # what was measured: touched ids, the member's result, before/after codes, re-location used
    seconds: float
    pairs: list[PairResult] = field(default_factory=list)  # body_overlap clusters only

    @property
    def outcome(self) -> Literal["gone", "still_there", "new_defect"]:
        """The trace's word for it (owner note 2): new defect wins over gone, since a repair that broke something
        else is not kept."""
        if self.new_hard:
            return "new_defect"
        return "gone" if self.gone else "still_there"

    def line(self) -> str:
        """The local-result third of the trace line: `gone (1.2 s)`, `new defect: open_shell on B3 (0.8 s)`."""
        if self.new_hard:
            what = ", ".join(f"{f['code']} on {'/'.join(f.get('entities') or ['?'])}" for f in self.new_hard)
            return f"new defect: {what} ({self.seconds:.1f} s)"
        return f"{'gone' if self.gone else 'still there'} ({self.seconds:.1f} s)"


def touched_bodies(pool: WorkerPool, ws_before: Workspace, ws_after: Workspace, defect: Defect,
                   *, pair: tuple[StableId, StableId] | None = None) -> list[StableId]:
    """The solids (stable ids on `ws_after`) a local check of this repair must look at: every body the ops between
    `ws_before` and `ws_after` modified or generated, the defect's own bodies (re-located), and, for body_overlap,
    every body whose box meets them (a repair that pushes a clash onto a neighbour must be seen). With `pair`, the
    pair's two bodies + their neighbours. Sorted, deterministic."""
    raise NotImplementedError("C17: interface published first; the build lands next")


def relocate(pool: WorkerPool, ws_before: Workspace, ws_after: Workspace, defect: Defect) -> Defect | None:
    """`defect` with its entities (and pairs) carried from `ws_before` to `ws_after`: through the ops' history where
    it names them (`modified` -> successors, `generated` -> kept + generated, `removed` -> dropped), else by the
    defect's points / box on `ws_after`. Returns None when nothing of it can be located on `ws_after`; the arm then
    re-checks locally and records `resolved_by_earlier_repair` if it is gone. `detail['relocated_by']` on the
    result of `verify_local` says which way was used."""
    raise NotImplementedError("C17: interface published first; the build lands next")


def verify_local(pool: WorkerPool, ws_before: Workspace, ws_after: Workspace, defect: Defect,
                 touched: list[StableId] | None = None, *, pair: tuple[StableId, StableId] | None = None,
                 timeout_s: float) -> LocalResult:
    """Did the repair from `ws_before` to `ws_after` fix `defect`, locally?

    `defect` is as diagnosed (on `ws_before` or earlier); it is re-located onto `ws_after` here. `touched` overrides
    `touched_bodies` (ids on `ws_after`). `pair` selects one pair of a body_overlap cluster; every pair of the
    cluster among the touched bodies is reported in `pairs`. `timeout_s` bounds the whole call; a mesher member runs
    under the panel's member timeout and the doomed cap.

    Raises `verdict.valid.CheckerUnavailable` when no local answer can be had (a worker timeout or crash): that is
    infrastructure, never a finding, exactly as for the full verdict.
    """
    raise NotImplementedError("C17: interface published first; the build lands next")
