# Responsibility: The body-level healing ops - `conformalise` and `remove_duplicate_faces` - as Op
#                 protocol objects the search can propose.
# Owns: the two op classes, their parameter schemas, `OPS`, and applying a kernel result to a
#       Workspace (declines included).
# Boundaries: no OCCT here; geometry runs in a worker via `body_kernel`. Registration in the op
#             registry is done at integration from `OPS`. `fuse` and `trim_unbounded` joined in lane C7.
"""Body ops: share faces between touching bodies; drop coincident duplicate faces.

Both return `(workspace, diff)` like every op. A DECLINE (an overlap outside the fidelity bound, a
pair that is not coincident, a result that would change the body count, ...) returns the workspace
unchanged and a Diff whose `params` carry `"declined": "<reason>"` and no changes.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from meshpipeline.cad.repair_v4.ops import OpFailed
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace, apply_history

_KIND = {"F": "face", "E": "edge", "B": "solid"}
_ID = r"^[A-Z][0-9]+$"


def _new_path(ws: Workspace, op: str) -> str:
    base = Path(ws.brep_path)
    return str(base.with_name(f"{base.stem}.{op}-{uuid.uuid4().hex[:8]}.brep"))


def _ids_by_index(ws: Workspace) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {"face": {}, "edge": {}}
    for sid, index in ws.indices.items():
        kind = _KIND[sid[0]]
        if kind in out:
            out[kind][str(index)] = sid
    return out


def _index(ws: Workspace, sid: StableId, prefix: str) -> int:
    if not sid.startswith(prefix):
        raise ValueError(f"{sid!r} is not a {_KIND[prefix]} id")
    return int(ws.indices[sid])


def _run(pool: WorkerPool, ws: Workspace, fn: str, payload: dict, timeout_s: float) -> dict:
    payload = {**payload, "brep_path": ws.brep_path, "quantum": ws.quantum, "part_diag_mm": ws.part_diag_mm,
               "out_path": _new_path(ws, fn), "ids": _ids_by_index(ws)}
    result = pool.call(fn, payload, timeout_s=timeout_s)
    if not result.ok or result.value is None:
        raise OpFailed(result.status, f"{fn}: {result.error or 'no answer'}")
    return result.value


def _apply(ws: Workspace, op: str, params: dict, value: dict) -> tuple[Workspace, Diff]:
    if "declined" in value:
        diff = Diff(op=op, params={**params, "declined": value["declined"],
                                   "detail": value.get("detail", {})},
                    modified={}, generated={}, removed=[], declared_added=[], declared_removed=[],
                    seconds=0.0)
        return ws, diff
    new = apply_history(ws, value["record"])
    return new, new.history[-1]


class Conformalise:
    name = "conformalise"
    params_schema: dict = {
        "type": "object",
        "properties": {
            # 0.1 mm is the absolute cap; the per-part bound min(0.1, 0.001*diagonal) is enforced
            # (as a decline) in the kernel, where the diagonal is known.
            "fuzzy_mm": {"type": "number", "minimum": 0},
            "mode": {"type": "string", "pattern": r"^(fragment|cut_by:[A-Z][0-9]+)$"},
        },
        "required": ["mode"],
        "additionalProperties": False,
    }

    def applies_to(self, site: Site) -> bool:
        return site.kind in ("overlap", "intersection", "proximity")

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict,
              *, timeout_s: float) -> tuple[Workspace, Diff]:
        ws.resolve(targets)
        mode = params["mode"]
        if len(set(targets)) < 2:
            raise ValueError("conformalise needs at least two bodies")
        cut_by = None
        if mode != "fragment":
            keeper = mode.removeprefix("cut_by:")
            if mode == keeper or keeper not in targets:
                raise ValueError(f"mode {mode!r} must name one of the bodies being conformalised")
            cut_by = _index(ws, keeper, "B")
        payload = {"bodies": [_index(ws, t, "B") for t in targets], "body_ids": list(targets),
                   "mode": mode, "cut_by": cut_by, "fuzzy_mm": float(params.get("fuzzy_mm", 0.0))}
        value = _run(pool, ws, "v4_conformalise", payload, timeout_s)
        return _apply(ws, self.name, {"bodies": list(targets), "mode": mode,
                                      "fuzzy_mm": payload["fuzzy_mm"]}, value)


class RemoveDuplicateFaces:
    name = "remove_duplicate_faces"
    params_schema: dict = {
        "type": "object",
        "properties": {
            "pairs": {"type": "array", "minItems": 1,
                      "items": {"type": "array", "minItems": 2, "maxItems": 2,
                                "items": {"type": "string", "pattern": _ID}}},
        },
        "required": ["pairs"],
        "additionalProperties": False,
    }

    def applies_to(self, site: Site) -> bool:
        # proximity and self_overlap too (lane A11): a measured gap-0 contact and gmsh's overlapping_facets pair are
        # both "two faces in the same place", which is exactly what this removes - and the sweep now proposes it there.
        return site.kind in ("overlap", "intersection", "proximity", "self_overlap")

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict,
              *, timeout_s: float) -> tuple[Workspace, Diff]:
        """`targets` are the faces named by `params["pairs"]`; the pairs are what is acted on."""
        pairs = [list(p) for p in params["pairs"]]
        ws.resolve([f for p in pairs for f in p])
        payload = {"pairs": [[_index(ws, a, "F"), _index(ws, b, "F")] for a, b in pairs],
                   "pair_ids": pairs}
        value = _run(pool, ws, "v4_remove_duplicate_faces", payload, timeout_s)
        return _apply(ws, self.name, {"pairs": pairs}, value)


class Fuse:
    """Fuse 2+ bodies into one. The no-fuse rule was removed by the owner's 2026-11-24 goal (lane C7): clean is valid +
    panel, faithfulness (the body count among it) is reported, never a gate."""

    name = "fuse"
    params_schema: dict = {
        "type": "object",
        "properties": {"fuzzy_mm": {"type": "number", "minimum": 0}},
        "required": [],
        "additionalProperties": False,
        "x-targets": {"kind": "solid", "min": 2, "max": None},
    }

    def applies_to(self, site: Site) -> bool:
        return site.kind in ("overlap", "intersection")

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict,
              *, timeout_s: float) -> tuple[Workspace, Diff]:
        ws.resolve(targets)
        if len(set(targets)) < 2:
            raise ValueError("fuse needs at least two bodies")
        payload = {"bodies": [_index(ws, t, "B") for t in targets], "body_ids": list(targets),
                   "fuzzy_mm": float(params.get("fuzzy_mm", 0.0))}
        value = _run(pool, ws, "v4_fuse", payload, timeout_s)
        return _apply(ws, self.name, {"bodies": list(targets), "fuzzy_mm": payload["fuzzy_mm"]}, value)


class TrimUnbounded:
    """Bound the part's infinite surfaces to its real extent (Common with its finite-face box, enlarged by
    `pad_fraction` of the diagonal). After Raymond Lee's approach in raymond/cad-simplification (devtools/simplify).
    The faces the box adds are declared."""

    name = "trim_unbounded"
    params_schema: dict = {
        "type": "object",
        "properties": {"pad_fraction": {"type": "number", "minimum": 0.001, "maximum": 0.25}},
        "required": ["pad_fraction"],
        "additionalProperties": False,
        "x-targets": {"kind": None, "min": 0, "max": 0},
    }

    def applies_to(self, site: Site) -> bool:
        return site.kind == "unbounded"

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict,
              *, timeout_s: float) -> tuple[Workspace, Diff]:
        if targets:
            raise ValueError("trim_unbounded takes no targets: it trims the whole part")
        value = _run(pool, ws, "v4_trim_unbounded", {"pad_fraction": float(params["pad_fraction"])}, timeout_s)
        new, diff = _apply(ws, self.name, {"pad_fraction": float(params["pad_fraction"])}, value)
        added = set(diff.params.pop("added_face_indices", []) or [])
        diff.declared_added = sorted(sid for sid, i in new.indices.items() if sid[:1] == "F" and i in added)
        return new, diff


#: Listed for the registry, which is wired at integration. `fuse` joined in lane C7 (the no-fuse rule is gone).
OPS: tuple[Any, ...] = (Conformalise(), RemoveDuplicateFaces(), Fuse(), TrimUnbounded())
