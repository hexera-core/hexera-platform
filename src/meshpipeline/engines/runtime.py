# Responsibility: Resolve an engine name to its live runtime module.
# Boundaries: the one place a bundle is imported by name.
# Collaborates with: engines/registry.py and each bundle's adapter.
from __future__ import annotations

import importlib
import logging

from meshpipeline.engines.base import MeshEngine  # noqa: F401  (adapter return type)

logger = logging.getLogger(__name__)

_instances: dict[str, object] = {}


def get_engine(name: str = "") -> MeshEngine:
    from meshpipeline.engines.registry import UnknownEngineError, default_engine
    key = (name or default_engine()).lower()
    inst = _instances.get(key)
    if inst is not None:
        return inst
    try:
        mod = importlib.import_module(f"meshpipeline.engines.{key}.adapter")
        inst = mod.ENGINE()
    except (ImportError, AttributeError) as exc:
        raise UnknownEngineError(
            f"unknown engine {name!r}: no engines/{key}/adapter.py with an ENGINE class"
        ) from exc
    _instances[key] = inst
    return inst
