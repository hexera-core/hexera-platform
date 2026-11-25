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

import os
import tempfile
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from itertools import product
from typing import Any, Literal

from meshpipeline.cad.repair_v4.types import StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Workspace

#: Box = (xmin, ymin, zmin, xmax, ymax, zmax), mm.
Box = tuple[float, float, float, float, float, float]

#: how long one local kernel call may take (s); the arm's own wall bounds the sum
CALL_TIMEOUT_S = 600.0


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


def _new_diffs(ws_before: Workspace, ws_after: Workspace) -> list:
    """The ops between the two states. `ws_after` must descend from `ws_before` (its history extends it)."""
    n = len(ws_before.history)
    if len(ws_after.history) < n or ws_after.history[:n] != ws_before.history:
        raise ValueError("ws_after does not descend from ws_before: their histories diverge")
    return list(ws_after.history[n:])


def _forward(ids: Iterable[StableId], diffs: list) -> set[StableId]:
    """Stable ids carried through `diffs`: a modified id becomes its successors, a removed one goes, a generating one
    stays and adds what it generated."""
    current = set(ids)
    for diff in diffs:
        nxt: set[StableId] = set()
        for sid in current:
            if sid in diff.modified:
                nxt.update(diff.modified[sid])
            elif sid not in diff.removed:
                nxt.add(sid)
            nxt.update(diff.generated.get(sid, ()))
        current = nxt
    return current


def _changed(diffs: list) -> set[StableId]:
    """Every id the ops made or changed: successors of modified ids, generated ids and their generators."""
    out: set[StableId] = set()
    for diff in diffs:
        for news in diff.modified.values():
            out.update(news)
        for old, news in diff.generated.items():
            out.add(old)
            out.update(news)
    return out


def _live(ws: Workspace, ids: Iterable[StableId], prefix: str) -> list[int]:
    return sorted({ws.indices[s] for s in ids if s.startswith(prefix) and s in ws.indices})


def _body_of_index(ws: Workspace) -> dict[int, StableId]:
    return {i: s for s, i in ws.indices.items() if s.startswith("B")}


def _kernel(pool: WorkerPool, fn: str, payload: dict, timeout_s: float) -> dict:
    from meshpipeline.cad.repair_v4.verdict.valid import CheckerUnavailable

    result = pool.call(fn, payload, timeout_s=min(timeout_s, CALL_TIMEOUT_S))
    if not result.ok or result.value is None:
        raise CheckerUnavailable(f"{fn}: {result.status}: {result.error}")
    return result.value


def _relocate(pool: WorkerPool, ws_before: Workspace, ws_after: Workspace, defect: Defect,
              timeout_s: float) -> tuple[Defect | None, str]:
    diffs = _new_diffs(ws_before, ws_after)
    if not defect.entities:
        return defect, "whole_part"
    carried = sorted(_forward(defect.entities, diffs) & set(ws_after.ids))
    if carried:
        pairs = tuple(sorted({tuple(sorted((a, b))) for p in defect.pairs
                              for a, b in product(sorted(_forward([p[0]], diffs)), sorted(_forward([p[1]], diffs)))
                              if a != b and a in ws_after.ids and b in ws_after.ids}))
        how = "unchanged" if not diffs or set(carried) == set(defect.entities) else "history"
        return replace(defect, entities=tuple(carried), pairs=pairs), how  # type: ignore[arg-type]
    points = list(defect.xyz)
    if defect.box is not None:
        x0, y0, z0, x1, y1, z1 = defect.box
        points.append(((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2))
    if not points:
        return None, "lost"
    tol = 1e-3 * (ws_after.part_diag_mm or 1.0)
    found = _kernel(pool, "locate_points", {"brep": ws_after.brep_path, "xyz": [list(p) for p in points],
                                             "tol_mm": tol}, timeout_s)["solids"]
    bodies = _body_of_index(ws_after)
    located = tuple(sorted(bodies[i] for i in found if i in bodies))
    if not located:
        return None, "lost"
    return replace(defect, entities=located, pairs=()), "geometry"


def relocate(pool: WorkerPool, ws_before: Workspace, ws_after: Workspace, defect: Defect) -> Defect | None:
    """`defect` with its entities (and pairs) carried from `ws_before` to `ws_after`: through the ops' history where
    it names them (`modified` -> successors, `generated` -> kept + generated, `removed` -> dropped), else by the
    defect's points / box on `ws_after` (bodies only). None when nothing of it can be located on `ws_after`; the arm
    then re-checks locally (`verify_local` does, on the touched bodies) and records `resolved_by_earlier_repair` if it
    is gone. A defect with no entities (whole part) is returned as is."""
    return _relocate(pool, ws_before, ws_after, defect, CALL_TIMEOUT_S)[0]


def _scope(ws_before: Workspace, ws_after: Workspace, defect: Defect | None,
           pair: tuple[StableId, StableId] | None) -> set[StableId]:
    """Stable ids on `ws_after` (bodies and faces) a local check starts from, before neighbours are added."""
    diffs = _new_diffs(ws_before, ws_after)
    scope = _changed(diffs) & set(ws_after.ids)
    if pair is not None:
        scope |= set(_forward(pair, diffs)) & set(ws_after.ids)
    elif defect is not None:
        scope |= set(defect.entities) & set(ws_after.ids)
    return scope


def touched_bodies(pool: WorkerPool, ws_before: Workspace, ws_after: Workspace, defect: Defect,
                   *, pair: tuple[StableId, StableId] | None = None) -> list[StableId]:
    """The solids (stable ids on `ws_after`) a local check of this repair must look at: every body the ops between
    `ws_before` and `ws_after` modified or generated (a changed face counts its body), the defect's own bodies
    (re-located), and, for body_overlap, every body whose box meets them (a repair that pushes a clash onto a
    neighbour must be seen). With `pair`, the pair's two bodies + their neighbours. Sorted, deterministic."""
    located, _how = _relocate(pool, ws_before, ws_after, defect, CALL_TIMEOUT_S)
    scope = _scope(ws_before, ws_after, located, pair)
    if not scope:
        return sorted(s for s in ws_after.ids if s.startswith("B"))
    got = _kernel(pool, "local_check", {"brep": ws_after.brep_path, "solids": _live(ws_after, scope, "B"),
                                        "faces": _live(ws_after, scope, "F"), "rules": False,
                                        "neighbours": defect.code == "body_overlap"}, CALL_TIMEOUT_S)
    bodies = _body_of_index(ws_after)
    return sorted(bodies[i] for i in [*got["solids"], *got["neighbours"]] if i in bodies)


Finding = tuple[str, tuple[StableId, ...]]


def _findings(ws: Workspace, got: dict) -> set[Finding]:
    """A `local_check` result as (code, stable ids) on `ws`."""
    bodies = _body_of_index(ws)
    out: set[Finding] = set()
    for index, codes in got["codes"].items():
        sid = bodies.get(int(index), f"#S{index}")
        out.update((code, (sid,)) for code in codes)
    for item in got["overlaps"]:
        i, j = item["pair"]
        out.add(("body_overlap", tuple(sorted((bodies.get(i, f"#S{i}"), bodies.get(j, f"#S{j}"))))))
    return out


def _carry(findings: set[Finding], diffs: list) -> set[Finding]:
    """`ws_before` findings expressed on `ws_after`'s ids: each id through the history (a body split in two carries
    its finding to both halves)."""
    out: set[Finding] = set()
    for code, ids in findings:
        successors = [sorted(_forward([sid], diffs)) or [sid] for sid in ids]
        for combo in product(*successors):
            out.add((code, tuple(sorted(combo))))
    return out


_BEFORE: dict[tuple, Any] = {}
_BEFORE_LOCK = threading.Lock()
_BEFORE_MAX = 256


def _remembered(key: tuple, compute: Any) -> Any:
    """The `ws_before` side is the same for every alternative the arm tries on one defect: measure it once."""
    with _BEFORE_LOCK:
        if key in _BEFORE:
            return _BEFORE[key]
    value = compute()
    with _BEFORE_LOCK:
        if len(_BEFORE) >= _BEFORE_MAX:
            _BEFORE.pop(next(iter(_BEFORE)))
        _BEFORE[key] = value
    return value


def _brep_key(path: str) -> tuple:
    stat = os.stat(path)
    return (path, stat.st_mtime_ns, stat.st_size)


def _member(name: str) -> Any:
    from meshpipeline.cad.repair_v4.verdict.assemble import default_members

    for member in default_members("full"):
        if member.name == name:
            return member
    raise ValueError(f"no panel member named {name!r}")


def _mesh_region(pool: WorkerPool, brep: str, member: str) -> Any:
    """The failing member on the exported touched solids alone, under the member timeout and the doomed cap."""
    from meshpipeline.cad.repair_v4.stages import step_for
    from meshpipeline.cad.repair_v4.verdict.assemble import DOOMED_CAP_S, MEMBER_TIMEOUT_S
    from meshpipeline.cad.repair_v4.verdict.panel import run_panel

    step = step_for(pool, brep)
    return run_panel(pool, step, [_member(member)], timeout_s=MEMBER_TIMEOUT_S, doomed_cap_s=DOOMED_CAP_S)[0]


def verify_local(pool: WorkerPool, ws_before: Workspace, ws_after: Workspace, defect: Defect,
                 touched: list[StableId] | None = None, *, pair: tuple[StableId, StableId] | None = None,
                 timeout_s: float) -> LocalResult:
    """Did the repair from `ws_before` to `ws_after` fix `defect`, locally?

    `defect` is as diagnosed on `ws_before`; it is re-located onto `ws_after` here. `touched` overrides
    `touched_bodies` (ids on `ws_after`). `pair` selects one pair of a body_overlap cluster; every pair of the
    cluster among the touched bodies is reported in `pairs`. `timeout_s` bounds each kernel call; a mesher member runs
    under the panel's member timeout and the doomed cap.

    How it decides:
    - layer 1's per-solid rules (and overlap against box-neighbours, for body_overlap) run on the touched solids of
      `ws_after`, and on their PRE-IMAGES in `ws_before`; the before findings are carried onto `ws_after`'s ids
      through the history. `new_hard` = after findings not among the carried before ones, other than this defect's
      own code on its own entities. `gone` = this defect's code no longer on its (re-located) entities, or on the
      selected `pair`.
    - a mesher defect: additionally the member that failed meshes the touched solids alone; `gone` = it passes.
      `detail['region_passed_before']` says whether it already passed on the same region of `ws_before`: then the
      local mesh cannot see this defect (it needs the rest of the part), and only the full check can.

    Raises `verdict.valid.CheckerUnavailable` when no local answer can be had (a worker timeout or crash): that is
    infrastructure, never a finding, exactly as for the full verdict.
    """
    started = time.monotonic()
    diffs = _new_diffs(ws_before, ws_after)
    located, how = _relocate(pool, ws_before, ws_after, defect, timeout_s)
    overlap = defect.code == "body_overlap"
    if touched is not None:
        scope = set(touched) & set(ws_after.ids)
    else:
        scope = _scope(ws_before, ws_after, located, pair)
    if not scope:  # nothing changed and nothing located: the whole part is the region
        scope = {s for s in ws_after.ids if s.startswith("B")}
    work = tempfile.mkdtemp(prefix="local_verify_")
    region = os.path.join(work, "region.brep")
    after = _kernel(pool, "local_check", {
        "brep": ws_after.brep_path, "solids": _live(ws_after, scope, "B"), "faces": _live(ws_after, scope, "F"),
        "neighbours": overlap, "diag_mm": ws_after.part_diag_mm,
        "export": region if defect.kind == "mesher" else None}, timeout_s)
    bodies_after = _body_of_index(ws_after)
    region_ids = sorted(bodies_after[i] for i in after["solids"] if i in bodies_after)

    # the same region on ws_before: every live-before body whose successors meet the region (or the neighbours)
    after_group = {bodies_after[i] for i in [*after["solids"], *after["neighbours"]] if i in bodies_after}
    pre = sorted(s for s in ws_before.ids if s.startswith("B") and _forward([s], diffs) & after_group)
    if not pre and not diffs:
        pre = sorted(after_group)
    before_region = os.path.join(work, "region_before.brep")
    pre_solids = _live(ws_before, pre, "B")
    pre_payload = {"brep": ws_before.brep_path, "solids": pre_solids, "faces": [],
                   "neighbours": overlap, "diag_mm": ws_before.part_diag_mm,
                   "export": before_region if defect.kind == "mesher" else None}
    before = _remembered(("check", _brep_key(ws_before.brep_path), tuple(pre_solids), overlap),
                         lambda: _kernel(pool, "local_check", pre_payload, timeout_s))

    found_after = _findings(ws_after, after)
    carried = _carry(_findings(ws_before, before), diffs)
    own = set(located.entities) if located is not None else set(region_ids)
    selected = None
    if pair is not None:
        selected = {tuple(sorted(c)) for c in product(sorted(_forward([pair[0]], diffs)), sorted(_forward([pair[1]], diffs)))
                    if c[0] != c[1]}

    def is_this_defect(f: Finding) -> bool:
        if f[0] != defect.code:
            return False
        if selected is not None:
            return f[1] in selected
        return not own or bool(set(f[1]) & own) or not located or not located.entities

    still = [f for f in found_after if is_this_defect(f)]
    new_hard = [{"code": c, "entities": list(ids)} for c, ids in sorted(found_after - carried) if not is_this_defect((c, ids))]
    gone = not still
    detail: dict = {"relocated_by": how, "touched": region_ids,
                    "neighbours": sorted(bodies_after[i] for i in after["neighbours"] if i in bodies_after),
                    "before": sorted(f"{c} {'/'.join(i)}" for c, i in carried),
                    "after": sorted(f"{c} {'/'.join(i)}" for c, i in found_after),
                    "pairs_tested": len(after["tested_pairs"])}

    pairs: list[PairResult] = []
    if overlap and located is not None and located.pairs:
        live_overlaps: dict[tuple, dict] = {ids: next((o for o in after["overlaps"]
                                    if tuple(sorted((bodies_after.get(o["pair"][0], ""),
                                                     bodies_after.get(o["pair"][1], "")))) == ids), {})
                         for c, ids in found_after if c == "body_overlap"}
        for p in located.pairs:
            key = tuple(sorted(p))
            info = {k: v for k, v in live_overlaps.get(key, {}).items() if k != "pair"}
            pairs.append(PairResult(key, key not in live_overlaps, info))  # type: ignore[arg-type]

    if defect.kind == "mesher" and defect.member:
        result = _mesh_region(pool, region, defect.member)
        gone = result.status == "pass"
        before_result = _remembered(("mesh", _brep_key(ws_before.brep_path), tuple(pre_solids),
                                     defect.member),
                                    lambda: _mesh_region(pool, before_region, defect.member))
        detail.update(member=defect.member, member_status=result.status, member_error_kind=result.error_kind,
                      member_seconds=round(result.seconds, 2), region_passed_before=before_result.status == "pass")
    return LocalResult(gone, new_hard, detail, round(time.monotonic() - started, 3), pairs)
