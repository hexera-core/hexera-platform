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

    @register("sleep_forever")
    def _sleep_forever(payload: dict) -> dict:
        while True:
            time.sleep(3600)

    @register("abort")
    def _abort(payload: dict) -> dict:
        os.abort()


if os.environ.get("HEXERA_TEST_KERNEL_FNS") == "1":
    _register_test_fns()

# Registered for their side effect on REGISTRY; the workspace's kernel half. At the foot so that
# `register` and `REGISTRY` exist before the module that uses them is imported.
from meshpipeline.cad.repair_v4 import ws_kernel  # noqa: E402,F401

# TEMPORARY (Task 15): registers the shell ops' kernel functions in every worker. Task 11's
# ops/__init__ imports every op module, which makes this line redundant - delete it at integration.
from meshpipeline.cad.repair_v4.ops import shells  # noqa: E402,F401
