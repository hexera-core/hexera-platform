# Responsibility: The allow-list of kernel functions a v4 worker may run, by name.
# Owns: REGISTRY and `register`. A call is `fn(payload) -> value`, both JSON-serialisable dicts;
#       shapes appear in them as BREP file paths, never as OCCT objects.
# Boundaries: no dispatch, no process handling (see worker_pool). Later tasks add their functions
#             with one `register` line, in a module imported at the foot of this file.
"""The kernel-call registry. Only names in `REGISTRY` can be run by a `WorkerPool`."""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from meshpipeline.settings.cad_repair import kernel_test_fns_enabled

KernelFn = Callable[[dict], dict]

REGISTRY: dict[str, KernelFn] = {}


def register(name: str) -> Callable[[KernelFn], KernelFn]:
    """Decorator: add `fn` to `REGISTRY` under `name`."""

    def _add(fn: KernelFn) -> KernelFn:
        if name in REGISTRY:
            raise ValueError(f"kernel function {name!r} is already registered")
        REGISTRY[name] = fn
        return fn

    return _add


def _register_test_fns() -> None:
    @register("echo")
    def _echo(payload: dict) -> dict:
        return {"echo": payload, "pid": os.getpid()}

    @register("slow_echo")
    def _slow_echo(payload: dict) -> dict:
        time.sleep(float(payload.get("sleep_s", 0)))
        return {"echo": payload, "pid": os.getpid()}

    @register("sleep_forever")
    def _sleep_forever(payload: dict) -> dict:
        while True:
            time.sleep(3600)

    @register("abort")
    def _abort(payload: dict) -> dict:
        os.abort()


if kernel_test_fns_enabled():
    _register_test_fns()

# Kernel functions that need OCCT register themselves on import. They import OCP lazily, so these
# imports are safe on a host without it. At the foot so that `register` and `REGISTRY` exist
# before the modules that use them are imported.
# The op library: each op module registers its kernel function(s) here and its `Op` in
# `ops.REGISTRY`. One import covers every op module (see ops/__init__.py, which auto-discovers),
# so a per-op-module import here would be redundant.
from meshpipeline.cad.repair_v4 import (
    body_kernel,  # noqa: E402,F401
    loss_kernel,  # noqa: E402,F401
    ops,  # noqa: E402,F401
    sites_kernel,  # noqa: E402,F401
    ws_kernel,  # noqa: E402,F401
)
from meshpipeline.cad.repair_v4 import layer1_gate_kernel as _layer1_gate_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v4 import local_verify_kernel as _local_verify_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v4 import xcaf_io as _xcaf_io  # noqa: E402,F401
from meshpipeline.cad.repair_v4.verdict import faithful_kernel as _faithful_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v4.verdict import mesh_kernel as _mesh_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v4.verdict import netgen_member as _netgen_member  # noqa: E402,F401
from meshpipeline.cad.repair_v4.verdict import valid_kernel as _valid_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v4.verdict import versions_kernel as _versions_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v5 import agent_kernel as _v5_agent_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v5 import brep_facts_kernel as _v5_brep_facts_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v5 import close_faces_kernel as _v5_close_faces_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v5 import conformal_kernel as _v5_conformal_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v5 import crossing_kernel as _v5_crossing_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v5 import diagnose_kernel as _v5_diagnose_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v5 import facet_local_kernel as _v5_facet_local_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v5 import narrow_kernel as _v5_narrow_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v5 import open_shell_kernel as _v5_open_shell_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v5 import overlap_kernel as _v5_overlap_kernel  # noqa: E402,F401
from meshpipeline.cad.repair_v5 import quality_kernel as _v5_quality_kernel  # noqa: E402,F401
