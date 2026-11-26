# Responsibility: The state object a v4 search forks - a part on disk, stable ids for its entities,
#                 their labels, and the typed record of every op applied so far.
# Owns: Workspace, Diff, UnknownTarget, apply_history (BRepTools_History record -> new id map).
# Boundaries: no OCCT here (the parent never loads the kernel); shapes are BREP paths, and the
#             record `apply_history` reads is produced by `ws_kernel.history_record` in a worker.
"""Workspace, stable ids and diffs.

An entity's stable id ("F41") is minted once, when the entity is first seen, and then CARRIED across ops by the BRepTools_History the op's kernel function returns, so the proposer
can keep talking about geometry after an op has rebuilt it. Ids are never reused within a lineage:
a removed or replaced id stays dead, and `resolve` says which and why.

The history record (JSON, from `ws_kernel.history_record`). Entities are matched by KERNEL INDEX
(1-based, per kind, in the indexed map of the shape), never by fingerprint, because fingerprints
can collide (duplicate or coincident entities) and a collision must not mis-bind an id:

    {"version", "op", "params", "seconds", "declared_added", "declared_removed",
     "old_path",                                     # the BREP the op read: must be ws.brep_path
     "brep_path",                                    # the BREP the op wrote
     "entities": [{"type", "index", "fp"}, ...],     # every face/edge/solid of the NEW shape
     "history":  [{"type", "index", "fp",            # one row per OLD entity (fp checked vs ws)
                   "now",                            # its own index in the new shape, 0 if gone
                   "removed",
                   "modified":  [{"type", "index"}...],     # child indices in the NEW shape
                   "generated": [{"type", "index"}...]}, ...]}

Modified rules: an entity Modified into exactly ONE same-kind child that no other entity also
modifies into (1:1) KEEPS its id - a trimmed or healed face stays "face 41" - with its fingerprint
refreshed, its label carried and `Diff.modified[id] == [id]`. 1:N (a split) and N:M (a merge) retire
the parents and mint fresh ids for the children, which inherit the parents' labels.
`declared_added` / `declared_removed` are caller-supplied claims, passed through unchecked.

`apply_history` fails loudly (ValueError) on a wrong `version`, missing keys, an `old_path` that is
not `ws.brep_path`, or an old side that does not match the workspace (an id with no row, or a row
whose fingerprint differs). The kernel side also proves the written BREP reads back with the same
entity at every index (`ws_kernel.IndexInstabilityError`). Matching needs the op's untouched
geometry to be the SAME TShape in the new shape (`now`/child indices come from `FindIndex`); an op
that copies or relocates geometry makes every entity look vanished, so all ids are retired.
"""

from __future__ import annotations

import copy
import re
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
    #: live stable id -> that entity's index in `brep_path`'s per-kind indexed map
    indices: dict[StableId, int] = field(default_factory=dict)
    #: `ws_kernel.shape_signature` of `brep_path`: orientation, shell layout and closedness, which no entity
    #: fingerprint sees. Empty for a workspace the kernel never described (every fake in the unit tier).
    signature: tuple = ()
    #: the lineage's ORIGINAL bounding-box diagonal (mm), measured once by `ws_index` on the input and passed to every
    #: op as `part_diag_mm`, so relative thresholds never read a state's control-pole box (lane C13,
    #: `ws_kernel.part_diagonal`). None: not known; the kernel then measures the shape it is given.
    part_diag_mm: float | None = None

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
                 original=brep_path, quantum=float(result.value["quantum"]),
                 signature=tuple(result.value.get("signature") or ()),
                 part_diag_mm=(None if result.value.get("diag_mm") is None else float(result.value["diag_mm"])))
        for entity in result.value["entities"]:
            sid = ws._mint(entity["type"])
            ws.ids[sid] = entity["fp"]
            ws.indices[sid] = int(entity["index"])
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
                         counters=dict(self.counters), indices=dict(self.indices), signature=self.signature,
                         part_diag_mm=self.part_diag_mm)

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


RECORD_VERSION = 1  # keep equal to ws_kernel.RECORD_VERSION (the kernel module cannot be imported here)
_REQUIRED = ("version", "op", "params", "old_path", "brep_path", "seconds", "entities", "history")


def _check_record(ws: Workspace, rec: dict) -> None:
    """Refuse a record that is old-format, malformed, or not about THIS workspace's shape."""
    if not isinstance(rec, dict) or rec.get("version") != RECORD_VERSION:
        got = rec.get("version") if isinstance(rec, dict) else type(rec).__name__
        raise ValueError(f"history record version {got!r} is not {RECORD_VERSION}; the op must "
                         "emit ws_kernel.history_record's current format")
    absent = [k for k in _REQUIRED if k not in rec]
    if absent:
        raise ValueError(f"history record is missing {absent}")
    if rec["old_path"] != ws.brep_path:
        raise ValueError(f"history record was computed from {rec['old_path']!r} but the workspace "
                         f"is at {ws.brep_path!r}: stale workspace or an op that read another file")
    rows = {(r["type"], int(r["index"])): r for r in rec["history"]}
    for sid, fp in ws.ids.items():
        row = rows.get((_KIND[sid[0]], ws.indices[sid]))
        if row is None:
            raise ValueError(f"history record has no row for {sid} (index {ws.indices[sid]})")
        if row.get("fp") != fp:
            raise ValueError(f"history row for {sid} has fingerprint {row.get('fp')!r} but the "
                             f"workspace holds {fp!r}: the record is for a different shape")


def apply_history(ws: Workspace, history_json: dict) -> Workspace:
    """A NEW workspace after the op `history_json` describes; `ws` is not modified.

    Untouched entities and 1:1 Modified entities keep their ids; 1:N and N:M Modified children and
    Generated children get fresh ids and their parents' labels; removed entities' ids are retired.
    """
    missing = [sid for sid in ws.ids if sid not in ws.indices]
    if missing:
        raise ValueError(f"workspace has no kernel index for {missing}; build it with from_brep")
    _check_record(ws, history_json)
    new = ws.fork()
    by_old: dict[tuple[str, int], dict] = {
        (r["type"], int(r["index"])): r for r in history_json["history"]}
    entity: dict[tuple[str, int], str] = {
        (e["type"], int(e["index"])): e["fp"] for e in history_json["entities"]}
    unclaimed = set(entity)

    old = sorted(ws.ids, key=lambda s: (s[0], _number(s)))
    rows = {sid: by_old.get((_KIND[sid[0]], ws.indices[sid])) for sid in old}

    def kids_of(sid: StableId, field_name: str) -> list[tuple[str, int]]:
        row = rows[sid]
        return [] if row is None else [(c["type"], int(c["index"])) for c in row[field_name]]

    # Who modifies into each child: a child with one parent that has one child is a 1:1 case.
    parents_of: dict[tuple[str, int], list[StableId]] = {}
    for sid in old:
        for key in kids_of(sid, "modified"):
            parents_of.setdefault(key, []).append(sid)

    new_ids: dict[StableId, str] = {}
    new_indices: dict[StableId, int] = {}
    new_labels: dict = {}
    removed: list[StableId] = []
    modified: dict[StableId, list[StableId]] = {}
    generated: dict[StableId, list[StableId]] = {}
    minted: dict[tuple[str, int], StableId] = {}
    survivors: list[StableId] = []
    pending: list[StableId] = []

    def place(sid: StableId, key: tuple[str, int]) -> None:
        unclaimed.discard(key)
        new_ids[sid] = entity[key]
        new_indices[sid] = key[1]
        if sid in ws.labels:
            new_labels[sid] = copy.deepcopy(ws.labels[sid])

    for sid in old:
        row, kind = rows[sid], _KIND[sid[0]]
        kids = kids_of(sid, "modified")
        if row is None:
            removed.append(sid)  # the kernel gave no account of it: not silently kept
        elif len(kids) == 1 and kids[0][0] == kind and len(parents_of[kids[0]]) == 1 \
                and kids[0] in entity:
            place(sid, kids[0])  # 1:1 Modified: same entity, same id
            modified[sid] = [sid]
            if row["generated"]:
                survivors.append(sid)
        elif kids:
            pending.append(sid)
        elif row["removed"] or int(row["now"]) == 0 or (kind, int(row["now"])) not in entity:
            removed.append(sid)
        else:
            place(sid, (kind, int(row["now"])))
            survivors.append(sid)

    def child_ids(parent: StableId, keys: list[tuple[str, int]]) -> list[StableId]:
        out: list[StableId] = []
        for key in keys:
            if key not in entity or (key not in minted and key not in unclaimed):
                continue  # not in the new shape, or already owned by another id
            if key not in minted:
                minted[key] = new._mint(key[0])
                unclaimed.discard(key)
                new_ids[minted[key]] = entity[key]
                new_indices[minted[key]] = key[1]
            cid = minted[key]
            if cid not in out:
                out.append(cid)
            if parent in ws.labels:
                new_labels.setdefault(cid, copy.deepcopy(ws.labels[parent]))
        return out

    for sid in pending:
        modified[sid] = child_ids(sid, kids_of(sid, "modified"))
        gen = child_ids(sid, kids_of(sid, "generated"))
        if gen:
            generated[sid] = gen
    for sid in survivors:
        gen = child_ids(sid, kids_of(sid, "generated"))
        if gen:
            generated[sid] = gen

    # Whatever the new shape holds that no history row explains still gets a name.
    unattributed: list[StableId] = []
    for key in sorted(unclaimed):
        sid = new._mint(key[0])
        new_ids[sid] = entity[key]
        new_indices[sid] = key[1]
        unattributed.append(sid)

    params = dict(history_json.get("params") or {})
    if unattributed:
        params["unattributed"] = unattributed
    new.ids = new_ids
    new.indices = new_indices
    new.labels = new_labels
    new.brep_path = history_json["brep_path"]
    new.signature = tuple(history_json.get("signature") or ())
    new.history.append(Diff(
        op=history_json["op"], params=params, modified=modified, generated=generated,
        removed=removed, declared_added=list(history_json.get("declared_added", [])),
        declared_removed=list(history_json.get("declared_removed", [])),
        seconds=float(history_json.get("seconds", 0.0))))
    return new


__all__ = ["Diff", "StableId", "UnknownTarget", "Workspace", "apply_history"]
