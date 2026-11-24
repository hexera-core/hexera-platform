# Responsibility: The contract every v4 repair op meets, the registry that finds them, and the
#                 pre-flight check (`validate_call`) that turns a bad proposal into messages.
# Owns: `Op`, `REGISTRY`, `register`, `validate_call`, `run_kernel_op`, `OpDeclined`, `OpFailed`.
# Boundaries: no OCCT here and no geometry. An op module (split.py, bodies.py, ...) holds BOTH its
#             parent-side `Op` class and its kernel function; this file holds neither.
"""The v4 op protocol and registry.

ADDING AN OP never edits this file's logic. An op module does two things:

    from meshpipeline.cad.repair_v4.kernel_calls import register as register_kernel_fn
    from meshpipeline.cad.repair_v4.ops import register, run_kernel_op

    @register_kernel_fn("op_my_thing")            # runs in a worker; shapes are BREP paths
    def _my_thing(payload: dict) -> dict: ...      # returns ws_kernel.history_record(...)

    @register                                     # parent side: validates, calls the worker
    class MyThing:
        name = "my_thing"
        params_schema = {...}
        def applies_to(self, site): ...
        def apply(self, pool, ws, targets, params, *, timeout_s): ...

and nothing else: the foot of this file imports every module in this package. `kernel_calls`
imports this package, so the kernel function is registered in every worker too.

`params_schema` is a JSON Schema (the subset `validate_call` understands: type, enum, minimum,
maximum, minItems, maxItems, items, properties, required, additionalProperties; `register` refuses
a schema using anything else, so a bound is never silently unenforced). Two annotations
ride along as ordinary schema keywords, which any validator ignores:

    "x-entity": "face" | "edge" | "solid"   on a string: it must be a LIVE stable id of that kind
    "x-targets": {"kind": "face", "min": 1, "max": None}   at the top: what `targets` must hold

`validate_call` NEVER raises. The proposer will name a target a previous op removed; that comes
back as a message ("F7: removed by op 'split_angle'; it no longer exists") to retry with.
"""

from __future__ import annotations

import importlib
import math
import pkgutil
import uuid
from pathlib import Path
from typing import Any, Protocol, TypeVar

from meshpipeline.cad.repair_v4.types import Site, StableId
from meshpipeline.cad.repair_v4.worker_pool import WorkerPool
from meshpipeline.cad.repair_v4.workspace import (
    _KIND as _KIND_OF_PREFIX,
)
from meshpipeline.cad.repair_v4.workspace import Diff, UnknownTarget, Workspace, apply_history


class Op(Protocol):
    name: str
    #: JSON Schema (with bounds) for `params`; see the module docstring for the subset and the
    #: `x-entity` / `x-targets` annotations.
    params_schema: dict

    def applies_to(self, site: Site) -> bool:
        """Is this op a candidate for `site`? Cheap and pure; the search ranks the candidates."""
        ...

    def apply(self, pool: WorkerPool, ws: Workspace, targets: list[StableId], params: dict, *,
              timeout_s: float) -> tuple[Workspace, Diff]:
        """A NEW workspace and the Diff that produced it; `ws` is untouched. Raises `OpDeclined`
        when the op ran and chose to change nothing, `OpFailed` when the worker timed out, crashed
        or errored, and ValueError when `validate_call` would have rejected the call."""
        ...


class OpDeclined(Exception):
    """The op ran and declined: nothing was changed. `reason` is for the proposer."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class OpFailed(Exception):
    """The worker did not answer: `status` is the pool's ("error", "timeout" or "crashed")."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


REGISTRY: dict[str, Op] = {}

_C = TypeVar("_C", bound=type)


def register(cls: _C) -> _C:
    """Class decorator: instantiate `cls()` and file it in `REGISTRY` under its `name`. Refuses a
    `params_schema` that uses a keyword or type `validate_call` cannot enforce."""
    op = cls()
    problems = _unsupported(op.params_schema, "params_schema")
    if problems:
        raise ValueError(f"op {op.name!r}: " + "; ".join(problems))
    if op.name in REGISTRY:
        raise ValueError(f"op {op.name!r} is already registered")
    REGISTRY[op.name] = op
    return cls


# --- validate_call ------------------------------------------------------------------------------

_TYPES: dict[str, Any] = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}
#: Keywords `_check` enforces, plus annotations it knows are inert. Anything else is refused at
#: registration, because ignoring (say) `exclusiveMinimum` would mean an unenforced bound.
_ENFORCED = {"type", "enum", "minimum", "maximum", "minItems", "maxItems", "items", "properties",
             "required", "additionalProperties"}
_INERT = {"description", "title", "default"}


def _unsupported(schema: Any, path: str) -> list[str]:
    if not isinstance(schema, dict):
        return [f"{path}: a schema must be an object"]
    out: list[str] = []
    for key in schema:
        if key in _ENFORCED or key in _INERT or key.startswith("x-"):
            continue
        out.append(f"{path}: keyword {key!r} is not supported by validate_call")
    types = schema.get("type", [])
    for t in types if isinstance(types, list) else [types]:
        if t not in _TYPES and t not in ("number", "integer"):
            out.append(f"{path}: type {t!r} is not supported by validate_call")
    if "items" in schema:
        out += _unsupported(schema["items"], f"{path}.items")
    for key, sub in (schema.get("properties") or {}).items():
        out += _unsupported(sub, f"{path}.properties.{key}")
    return out


def _type_ok(value: Any, expected: str) -> bool:
    if expected in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return expected == "number" or float(value).is_integer()
    if expected not in _TYPES:
        return False
    return isinstance(value, _TYPES[expected])


def _check(value: Any, schema: dict, path: str, ws: Workspace, errors: list[str]) -> None:
    expected = schema.get("type")
    if expected is not None:
        options = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(value, t) for t in options):
            errors.append(f"{path}: expected {' or '.join(map(str, options))}, "
                          f"got {type(value).__name__}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            errors.append(f"{path}: {value} is not a finite number")
            return
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} is above the maximum {schema['maximum']}")
    if isinstance(value, str) and "x-entity" in schema:
        _check_entity(value, schema["x-entity"], path, ws, errors)
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items, got {len(value)}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: allows at most {schema['maxItems']} items, got {len(value)}")
        for i, item in enumerate(value):
            _check(item, schema.get("items", {}), f"{path}[{i}]", ws, errors)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", ()):
            if key not in value:
                errors.append(f"{path}: missing required parameter {key!r}")
        for key, item in value.items():
            if key in props:
                _check(item, props[key], f"{path}.{key}", ws, errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unknown parameter {key!r} (allowed: {sorted(props)})")


def _a(kind: str) -> str:
    return f"an {kind}" if kind[0] in "aeiou" else f"a {kind}"


def _check_entity(sid: str, kind: str | None, path: str, ws: Workspace, errors: list[str]) -> None:
    try:
        ws.resolve([sid])
    except UnknownTarget as gone:
        errors.append(f"{path}: {gone}")
        return
    # "any" (remove_small, sew) means any live entity, not a kind named "any": read literally it refused every
    # target ("F2 is a face, not an any"), so remove_small could never run and sew never with targets
    if kind is not None and kind != "any" and _KIND_OF_PREFIX.get(sid[:1]) != kind:
        errors.append(f"{path}: {sid} is {_a(_KIND_OF_PREFIX.get(sid[:1], 'unknown'))}, "
                      f"not {_a(kind)}")


def validate_call(name: str, targets: list[StableId], params: dict, ws: Workspace) -> list[str]:
    """Everything wrong with a proposed call, as messages; empty means it may be applied.

    Checks: the op exists; every target is live (a removed or replaced one says which op did it)
    and of the kind the op's `x-targets` asks for, in the count it allows; `params` meets the
    schema's types, enums and bounds (non-finite numbers are refused); every `x-entity` id inside
    params is live and of that kind; and, if the op defines the optional hook
    `extra_errors(targets, params) -> list[str]`, its cross-field rules (run only once the rest
    is clean, so it may assume well-typed params).
    """
    op = REGISTRY.get(name)
    if op is None:
        return [f"unknown op {name!r}; known ops: {sorted(REGISTRY)}"]
    errors: list[str] = []
    spec = op.params_schema.get("x-targets") or {}
    if not isinstance(targets, list) or not all(isinstance(t, str) for t in targets):
        errors.append("targets: expected a list of stable ids")
    else:
        for i, sid in enumerate(targets):
            _check_entity(sid, spec.get("kind"), f"targets[{i}]", ws, errors)
        if len(targets) < spec.get("min", 0):
            errors.append(f"targets: {name} needs at least {spec['min']}, got {len(targets)}")
        if spec.get("max") is not None and len(targets) > spec["max"]:
            errors.append(f"targets: {name} takes at most {spec['max']}, got {len(targets)}")
    if not isinstance(params, dict):
        errors.append("params: expected an object")
    else:
        _check(params, op.params_schema, "params", ws, errors)
        extra = getattr(op, "extra_errors", None)
        if extra is not None and isinstance(targets, list) and not errors:
            errors.extend(extra(targets, params))
    return errors


# --- the parent-side half of every kernel-backed op ---------------------------------------------


def check_call(name: str, targets: list[StableId], params: dict, ws: Workspace) -> None:
    """For `apply`: refuse (ValueError) a call `validate_call` rejects, rather than crash a worker."""
    errors = validate_call(name, targets, params, ws)
    if errors:
        raise ValueError(f"invalid call to {name}: " + "; ".join(errors))


def run_kernel_op(pool: WorkerPool, ws: Workspace, *, kernel_fn: str, op: str, payload: dict,
                  timeout_s: float) -> tuple[Workspace, Diff]:
    """Run `kernel_fn` on `ws`'s shape in a worker and fold its history record into a new workspace.

    The kernel function receives `payload` plus `brep_path` (the workspace's shape), `out_path` (a
    fresh file beside it to write the result to) and `quantum`, and returns either
    `ws_kernel.history_record(...)` or `{"declined": "<reason>"}`.
    """
    out_path = str(Path(ws.brep_path).parent / f"{op}-{uuid.uuid4().hex[:12]}.brep")
    result = pool.call(kernel_fn, {**payload, "brep_path": ws.brep_path, "out_path": out_path,
                                   "quantum": ws.quantum, "part_diag_mm": ws.part_diag_mm}, timeout_s=timeout_s)
    try:
        if not result.ok or result.value is None:
            raise OpFailed(result.status, result.error or "no answer")
        if "declined" in result.value:
            raise OpDeclined(str(result.value["declined"]))
        new = apply_history(ws, result.value)
    except BaseException:
        # A killed or declining worker may have left a partial `out_path`; nobody will read it.
        Path(out_path).unlink(missing_ok=True)
        raise
    return new, new.history[-1]


__all__ = ["Op", "OpDeclined", "OpFailed", "REGISTRY", "Site", "check_call", "register",
           "run_kernel_op", "validate_call"]

# Every sibling module of this package is imported for its `@register` side effect: a new op is a
# new file here, with no edit to this one. (A module whose name starts with "_" is skipped.)
for _module in sorted(m.name for m in pkgutil.iter_modules(__path__) if not m.name.startswith("_")):
    _mod = importlib.import_module(f"{__name__}.{_module}")
    # Tasks 12-15 were told not to edit this file while they ran in parallel, so their ops arrive
    # as a module-level `OPS` tuple rather than through `@register`. Both routes land in REGISTRY;
    # a module that uses neither contributes no ops, which `test_every_op_module_registers` catches.
    for _op in getattr(_mod, "OPS", ()):
        if _op.name in REGISTRY and REGISTRY[_op.name] is not _op:
            raise ValueError(f"op {_op.name!r} is already registered")
        REGISTRY[_op.name] = _op
