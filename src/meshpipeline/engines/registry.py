# Responsibility: Hold the catalog of shipped engines and answer which one a request may use.
# Owns: engine lookup, the default choice, and parameter validation against the engine's declared set.
# Boundaries: selection and validation only - it never runs an engine and never imports one's runner.
# Collaborates with: engines/base.py for the specification shape and engines/runtime.py for execution.
from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil

import meshpipeline.engines as _engines_pkg
from meshpipeline.engines.base import EngineSpec

logger = logging.getLogger(__name__)


def _discover() -> dict[str, EngineSpec]:
    specs: list[EngineSpec] = []
    for m in pkgutil.iter_modules(_engines_pkg.__path__):
        if not m.ispkg:
            continue                                   # base/registry/runtime/purposes
        if importlib.util.find_spec(f"meshpipeline.engines.{m.name}.spec") is None:
            continue     # a folder without spec.py is not an engine row
        spec = importlib.import_module(f"meshpipeline.engines.{m.name}.spec").SPEC
        if spec.name != m.name:
            raise RuntimeError(f"engines/{m.name}: SPEC.name is {spec.name!r} - "
                               "folder name and spec name must match")
        specs.append(spec)
    # implemented rows first (menu/UX order), then planned, alphabetical within
    specs.sort(key=lambda s: (not s.implemented, s.name))
    return {s.name: s for s in specs}


ENGINE_CATALOG: dict[str, EngineSpec] = _discover()

DEFAULT_ENGINE = "cfmesh"


def engine_names() -> list[str]:
    return [n for n, sp in ENGINE_CATALOG.items() if sp.implemented]


def all_engine_names() -> list[str]:
    return list(ENGINE_CATALOG.keys())


def default_engine() -> str:
    return DEFAULT_ENGINE


class UnknownEngineError(KeyError):
    pass


def get_spec(name: str) -> EngineSpec:
    key = (name or "").strip().lower()
    if not key:
        return ENGINE_CATALOG[DEFAULT_ENGINE]
    try:
        return ENGINE_CATALOG[key]
    except KeyError:
        raise UnknownEngineError(
            f"unknown engine {name!r} - known engines: {', '.join(sorted(ENGINE_CATALOG))}"
        ) from None


def spec_or_default(name: str) -> EngineSpec:
    try:
        return get_spec(name)
    except UnknownEngineError:
        logger.warning("spec_or_default: unknown engine %r → default %r", name, DEFAULT_ENGINE)
        return ENGINE_CATALOG[DEFAULT_ENGINE]


def engine_label(name: str) -> str:
    from meshpipeline.contracts.display_names import display_name
    return display_name("mesh_engine", name)


def catalog_menu() -> str:
    return "\n\n".join(
        f"{engine_label(s.name)}\n{s.descriptor}"
        for s in ENGINE_CATALOG.values() if s.implemented)


# engine_params: the declared-parameter machinery


def validate_engine_params(engine: str, given: dict) -> list[str]:
    return get_spec(engine).param_problems(given)


def resolve_engine_params(engine: str, given: dict | None) -> dict:
    spec = get_spec(engine)
    out: dict[str, str] = {}
    for p in spec.intake_params:
        v = str((given or {}).get(p.key, "") or "").strip().lower()
        if v and v not in p.values:
            # An EMPTY value defaulting is the intended "defaulted at dispatch" contract.
            # A NON-EMPTY value the engine does not declare is a caller mistake - resolving
            # it to the default anyway (so a direct dispatch never errors) is fine, but
            # doing it SILENTLY hides the mistake. Intake would have rejected it upstream;
            # only a programmatic/direct dispatch reaches here with a bad value.
            logger.warning("resolve_engine_params: %s param %r=%r not in %s - using default %r",
                           spec.name, p.key, v, list(p.values), p.default)
        out[p.key] = v if v in p.values else p.default
    return out


def engines_producing_topology(topo: str) -> list[str]:
    from meshpipeline.engines.purposes import engines_producing_topology as _impl
    return _impl(ENGINE_CATALOG, topo)


def engines_allowing(param_key: str, value: str) -> list[str]:
    return [n for n, sp in ENGINE_CATALOG.items() if sp.implemented
            and any(p.key == param_key and value in p.values
                    for p in sp.intake_params)]
