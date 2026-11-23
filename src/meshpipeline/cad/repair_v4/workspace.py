# Responsibility: The state object a v4 search forks - a part on disk, stable ids for its entities,
#                 their labels, and the typed record of every op applied so far.
# Owns: Workspace, Diff, UnknownTarget, apply_history (BRepTools_History record -> new id map).
# Boundaries: no OCCT here (the parent never loads the kernel); shapes are BREP paths, and the
#             record `apply_history` reads is produced by `ws_kernel.history_record` in a worker.
"""Workspace, stable ids and diffs.

An entity's stable id ("F41") is minted once, from its fingerprint (`identity.fingerprint_of`), and
then CARRIED across ops by the BRepTools_History the op's kernel function returns, so the proposer
can keep talking about geometry after an op has rebuilt it. Ids are never reused within a lineage:
a removed or replaced id stays dead, and `resolve` says which and why.

The history record (JSON, from `ws_kernel.history_record`):

    {"op", "params", "brep_path", "seconds", "declared_added", "declared_removed",
     "entities": [{"type", "fp"}, ...],             # every face/edge/solid of the NEW shape
     "history":  [{"type", "fp", "removed",          # one row per OLD entity
                   "modified": [{"type", "fp"}...], "generated": [{"type", "fp"}...]}, ...]}

Identity within a shape is the fingerprint, so two entities sharing a fingerprint are matched
first-come; the kernel cannot tell them apart either (`identity.EntityRef.ambiguous`).
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from dataclasses import dataclass, field

from meshpipeline.cad.repair_v4.types import StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool

_PREFIX = {"face": "F", "edge": "E", "solid": "B"}
_KIND = {v: k for k, v in _PREFIX.items()}


class UnknownTarget(Exception):
    """A proposed target id does not name a live entity. `reason` says whether it was removed,
    replaced (with its successors) or never existed, so the proposer can be told and retry."""

    def __init__(self, id: StableId, reason: str) -> None:
        super().__init__(f"{id}: {reason}")
        self.id = id
        self.reason = reason


@dataclass
class Diff:
    op: str
    params: dict
    #: old id -> the ids its Modified shapes got (the old id is gone)
    modified: dict[StableId, list[StableId]]
    #: old id -> the ids its Generated shapes got (the old id survives)
    generated: dict[StableId, list[StableId]]
    removed: list[StableId]
    #: what the op CLAIMED it would add/remove, for checking against the kernel's own history
    declared_added: list[StableId]
    declared_removed: list[StableId]
    seconds: float


def _number(stable_id: StableId) -> int:
    m = re.fullmatch(r"[A-Z](\d+)", stable_id)
    return int(m.group(1)) if m else 0


@dataclass
class Workspace:
    brep_path: str
    #: live stable id -> fingerprint of that entity in `brep_path`
    ids: dict[StableId, str]
    #: stable id -> label dict (e.g. {"name": ...}); children inherit their parent's
    labels: dict
    history: list[Diff]
    original: str
    #: the fingerprint quantum for this lineage; passed to every kernel call so names stay comparable
    quantum: float = 0.0
    #: highest number ever minted per prefix in this lineage; ids are never reused
    counters: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for sid in self.ids:
            self.counters[sid[0]] = max(self.counters.get(sid[0], 0), _number(sid))
        for diff in self.history:
            for sid in (*diff.modified, *diff.generated, *diff.removed,
                        *(c for kids in diff.modified.values() for c in kids),
                        *(c for kids in diff.generated.values() for c in kids)):
                self.counters[sid[0]] = max(self.counters.get(sid[0], 0), _number(sid))

    @classmethod
    def from_brep(cls, pool: WorkerPool, brep_path: str, labels: dict | None = None,
                  *, timeout_s: float = 120) -> Workspace:
        """Name every face, edge and solid of `brep_path` (in a worker) and start a lineage.

        `labels` is a stable-id -> label-dict map for the new ids; reading names from the STEP
        (xcaf_io) is the caller's job, so the workspace takes the map and does no I/O of its own.
        """
        result = pool.call("ws_index", {"brep_path": brep_path}, timeout_s=timeout_s)
        if not result.ok or result.value is None:
            raise RuntimeError(f"cannot index {brep_path!r}: {result.status}: {result.error}")
        ws = cls(brep_path=brep_path, ids={}, labels=copy.deepcopy(labels or {}), history=[],
                 original=brep_path, quantum=float(result.value["quantum"]))
        for entity in result.value["entities"]:
            ws.ids[ws._mint(entity["type"])] = entity["fp"]
        return ws

    def _mint(self, kind: str) -> StableId:
        prefix = _PREFIX[kind]
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}{self.counters[prefix]}"

    def fork(self) -> Workspace:
        """An independent copy: mutating it (ids, labels, history, counters) never touches `self`.
        The BREP file is shared because ops write NEW files rather than editing one."""
        return Workspace(brep_path=self.brep_path, ids=dict(self.ids),
                         labels=copy.deepcopy(self.labels), history=copy.deepcopy(self.history),
                         original=self.original, quantum=self.quantum,
                         counters=dict(self.counters))

    def resolve(self, ids: list[StableId]) -> list[StableId]:
        """`ids` unchanged if all are live; else UnknownTarget for the first that is not."""
        for sid in ids:
            if sid not in self.ids:
                raise UnknownTarget(sid, self._why_dead(sid))
        return list(ids)

    def _why_dead(self, sid: StableId) -> str:
        for diff in reversed(self.history):
            if sid in diff.removed:
                return f"removed by op '{diff.op}'; it no longer exists"
            if sid in diff.modified:
                kids = ", ".join(diff.modified[sid]) or "nothing"
                return f"replaced by op '{diff.op}' (now: {kids}); use the new ids"
        return "unknown id: it never existed in this workspace"


def apply_history(ws: Workspace, history_json: dict) -> Workspace:
    """A NEW workspace after the op `history_json` describes; `ws` is not modified.

    Entities the op left alone keep their ids; a Modified or Generated child gets a fresh id and
    its parent's label; a removed (or vanished) entity's id is retired.
    """
    new = ws.fork()
    rows: dict[tuple[str, str], list[dict]] = {}
    for row in history_json["history"]:
        rows.setdefault((row["type"], row["fp"]), []).append(row)
    pool = Counter((e["type"], e["fp"]) for e in history_json["entities"])

    old = sorted(ws.ids, key=lambda s: (s[0], _number(s)))
    new_ids: dict[StableId, str] = {}
    new_labels: dict = {}
    removed: list[StableId] = []
    survivors: list[tuple[StableId, dict | None]] = []

    def row_for(sid: StableId) -> dict | None:
        key = (_KIND[sid[0]], ws.ids[sid])
        return rows[key].pop(0) if rows.get(key) else None

    def keep(sid: StableId, fp: str) -> None:
        new_ids[sid] = fp
        if sid in ws.labels:
            new_labels[sid] = copy.deepcopy(ws.labels[sid])

    # Pass 1: untouched survivors claim their entity first, so a child can't steal it.
    pending: list[tuple[StableId, dict]] = []
    for sid in old:
        kind, fp = _KIND[sid[0]], ws.ids[sid]
        row = row_for(sid)
        replaced = row is not None and bool(row["modified"])
        if row is not None and replaced and any((c["type"], c["fp"]) == (kind, fp) for c in row["modified"]):
            replaced = False  # Modified to itself: the entity (and its id) survives
        if row is not None and row["removed"] and not row["modified"]:
            removed.append(sid)
        elif row is not None and replaced:
            pending.append((sid, row))
        elif pool[(kind, fp)] > 0:
            pool[(kind, fp)] -= 1
            keep(sid, fp)
            survivors.append((sid, row))
        else:
            removed.append(sid)  # the shape no longer holds it and history did not say why

    modified: dict[StableId, list[StableId]] = {}
    generated: dict[StableId, list[StableId]] = {}
    minted: dict[tuple[str, str], StableId] = {}

    def child_ids(parent: StableId, kids: list[dict]) -> list[StableId]:
        out: list[StableId] = []
        for kid in kids:
            key = (kid["type"], kid["fp"])
            if key == (_KIND[parent[0]], ws.ids[parent]):
                continue
            if key not in minted:
                if pool[key] <= 0:
                    continue
                pool[key] -= 1
                minted[key] = new._mint(kid["type"])
                new_ids[minted[key]] = kid["fp"]
            cid = minted[key]
            if cid not in out:
                out.append(cid)
            if parent in ws.labels:
                new_labels.setdefault(cid, copy.deepcopy(ws.labels[parent]))
        return out

    # Pass 2: replaced entities, then what survivors generated.
    for sid, row in pending:
        modified[sid] = child_ids(sid, row["modified"])
        gen = child_ids(sid, row["generated"])
        if gen:
            generated[sid] = gen
    for sid, row in survivors:
        if row and row["generated"]:
            gen = child_ids(sid, row["generated"])
            if gen:
                generated[sid] = gen

    # Whatever the new shape holds that no history row explains still gets a name.
    unattributed: list[StableId] = []
    for (kind, fp), count in sorted(pool.items()):
        for _ in range(max(count, 0)):
            sid = new._mint(kind)
            new_ids[sid] = fp
            unattributed.append(sid)

    params = dict(history_json.get("params") or {})
    if unattributed:
        params["unattributed"] = unattributed
    new.ids = new_ids
    new.labels = new_labels
    new.brep_path = history_json["brep_path"]
    new.history.append(Diff(
        op=history_json["op"], params=params, modified=modified, generated=generated,
        removed=removed, declared_added=list(history_json.get("declared_added", [])),
        declared_removed=list(history_json.get("declared_removed", [])),
        seconds=float(history_json.get("seconds", 0.0))))
    return new


__all__ = ["Diff", "StableId", "UnknownTarget", "Workspace", "apply_history"]
