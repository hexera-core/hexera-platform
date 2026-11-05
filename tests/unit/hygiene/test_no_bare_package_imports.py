# Responsibility: Verify the distribution is importable only through its namespace, with no former root on the path.
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import meshpipeline

# src/meshpipeline - the package directory (must never be on sys.path; only its parent `src` is).
PKG_DIR = Path(meshpipeline.__file__).resolve().parent

FORMER_ROOTS = [
    "engines", "agents", "agent_tools", "api", "application", "cad", "capture", "contracts",
    "core", "events", "persistence", "pipeline", "prompts", "render", "runtime", "sandbox",
    "settings", "adapters", "worker", "tasks", "errors", "metrics", "graph", "main",
]


def _resolves_into_package(name: str) -> bool:
    spec = importlib.util.find_spec(name)
    if spec is None:
        return False
    locations = list(spec.submodule_search_locations or [])
    if spec.origin and spec.origin != "namespace":
        locations.append(spec.origin)
    for loc in locations:
        p = Path(loc).resolve()
        if p == PKG_DIR or PKG_DIR in p.parents:
            return True
    return False


def test_package_dir_is_not_on_sys_path():
    on_path = [p for p in sys.path if Path(p).resolve() == PKG_DIR]
    assert not on_path, f"src/meshpipeline is on sys.path - internal packages leak as top-level: {on_path}"


def test_no_former_root_resolves_into_the_installed_package():
    leaked = sorted(n for n in set(FORMER_ROOTS) if _resolves_into_package(n))
    assert not leaked, (
        "these former internal roots resolve to product source under src/meshpipeline - the package "
        f"dir is on sys.path (a checkout-path bypass of the installed distribution): {leaked}")


def test_the_distribution_is_importable_through_its_namespace():
    assert importlib.util.find_spec("meshpipeline") is not None, \
        "the `meshpipeline` distribution is not importable - it must be installed (wheel or -e .)"
    # internal packages resolve ONLY under the namespace
    assert importlib.util.find_spec("meshpipeline.engines") is not None
    assert importlib.util.find_spec("meshpipeline.contracts") is not None
    assert importlib.util.find_spec("meshpipeline.settings.providers") is not None
