# Responsibility: the parent side of CRS v5's overlap_self_intersection ops (build step 8): `boolean_by_volume`,
#                 `fragment_drop_small`, `drop_duplicate_body` - schemas, validation, the worker call.
# Owns: the three Op classes and `OPS`.
# Boundaries: no OCCT here; the kernels are `repair_v5.overlap_kernel`. Applying a record (and a decline) is
#             bodies.py's `_run`/`_apply`, shared by every body op. Never proposed by v4's sweeps (`applies_to` is
#             False): only v5's class menu names them.
"""Overlap and self-intersection ops for the v5 tournament (see `repair_v5.overlap_kernel` for what each does)."""

from __future__ import annotations

from typing import Any

from meshpipeline.cad.repair_v4.ops.bodies import _apply, _index, _run
from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import Diff, Workspace

#: the volume-share line a pair is fused at rather than cut (= repair_v5.conformal.FUSE_SHARE, the pass's own rule)
FUSE_SHARE = 0.5


class BooleanByVolume:
    """ONE overlapping pair, resolved by a Boolean chosen by volume: a duplicate -> drop the copy; overlap >= half the
    smaller body -> fuse; else cut the smaller by the larger."""

    name = "boolean_by_volume"
    params_schema: dict = {
        "type": "object",
        "properties": {"fuse_share": {"type": "number", "minimum": 0.05, "maximum": 1.0}},
        "required": [],
        "additionalProperties": False,
        "x-targets": {"kind": "solid", "min": 2, "max": 2},
    }

    def applies_to(self, site: Site) -> bool:
        return False

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict,
              *, timeout_s: float) -> tuple[Workspace, Diff]:
        ws.resolve(targets)
        if len(set(targets)) != 2:
            raise ValueError("boolean_by_volume resolves exactly one pair of bodies")
        payload = {"bodies": [_index(ws, t, "B") for t in targets], "body_ids": list(targets),
                   "fuse_share": float(params.get("fuse_share", FUSE_SHARE))}
        value = _run(pool, ws, "v5_boolean_by_volume", payload, timeout_s)
        return _apply(ws, self.name, {"bodies": list(targets), "fuse_share": payload["fuse_share"]}, value)


class FragmentDropSmall:
    """Fragment the target bodies where they interfere (one self-intersecting body: rebuilt from its own faces) and
    drop every resulting solid under `max_fraction` of the part's volume."""

    name = "fragment_drop_small"
    params_schema: dict = {
        "type": "object",
        "properties": {"max_fraction": {"type": "number", "minimum": 0.0, "maximum": 0.05}},
        "required": ["max_fraction"],
        "additionalProperties": False,
        "x-targets": {"kind": "solid", "min": 1, "max": None},
    }

    def applies_to(self, site: Site) -> bool:
        return False

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict,
              *, timeout_s: float) -> tuple[Workspace, Diff]:
        ws.resolve(targets)
        if not targets:
            raise ValueError("fragment_drop_small needs at least one body")
        payload: dict[str, Any] = {"bodies": [_index(ws, t, "B") for t in targets], "body_ids": list(targets),
                                   "max_fraction": float(params["max_fraction"])}
        value = _run(pool, ws, "v5_fragment_drop_small", payload, timeout_s)
        return _apply(ws, self.name, {"bodies": list(targets), "max_fraction": payload["max_fraction"]}, value)


class DropDuplicateBody:
    """Drop the second of two bodies that carry the duplicate signature (one body written twice)."""

    name = "drop_duplicate_body"
    params_schema: dict = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
        "x-targets": {"kind": "solid", "min": 2, "max": 2},
    }

    def applies_to(self, site: Site) -> bool:
        return False

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict,
              *, timeout_s: float) -> tuple[Workspace, Diff]:
        ws.resolve(targets)
        if len(set(targets)) != 2:
            raise ValueError("drop_duplicate_body takes the kept body, then its copy")
        payload = {"bodies": [_index(ws, t, "B") for t in targets], "body_ids": list(targets)}
        value = _run(pool, ws, "v5_drop_duplicate_body", payload, timeout_s)
        return _apply(ws, self.name, {"bodies": list(targets)}, value)


OPS: tuple[Any, ...] = (BooleanByVolume(), FragmentDropSmall(), DropDuplicateBody())
