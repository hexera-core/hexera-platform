# Responsibility: Verify every shipped module imports.
from __future__ import annotations

# pyvista/PIL stand-ins are installed ONCE by tests/unit/conftest.py, and only where the
# real package is genuinely absent. Doing it per-module raced: whichever module was
# imported first decided, and one of them shadowed an installed pyvista.
import importlib
import sys
import traceback
import types as _types
from pathlib import Path

APP_DIR = Path(__file__).parent.parent.parent.parent / "src" / "meshpipeline"



try:
    import prometheus_fastapi_instrumentator  # noqa: F401
except ImportError:
    class _ChainAll:
        def __call__(self, *a, **k): return self
        def __getattr__(self, name): return self
    _pfi = _types.ModuleType("prometheus_fastapi_instrumentator")
    _pfi.Instrumentator = _ChainAll()
    sys.modules["prometheus_fastapi_instrumentator"] = _pfi


def _all_app_modules() -> list[str]:
    mods = []
    for p in sorted(APP_DIR.rglob("*.py")):
        rel = p.relative_to(APP_DIR)
        if "__pycache__" in rel.parts:
            continue
        name = "meshpipeline." + ".".join(rel.with_suffix("").parts)
        if name.endswith("__init__"):
            name = name[: -len(".__init__")]
        if name:
            mods.append(name)
    return mods


def test_every_module_imports():
    modules = _all_app_modules()
    assert modules, "the import sweep found no modules - the discovery walk is broken"

    failures: list[str] = []
    for name in modules:
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 - a module-level failure is any exception
            tb = traceback.format_exc(limit=8)
            failures.append(f"  {name}: {type(exc).__name__}: {exc}\n{tb}")

    assert not failures, (
        f"{len(failures)} of {len(modules)} modules failed to import "
        f"(a module-level SyntaxError/NameError, a circular import, or a missing import "
        f"guard hides from the unit suite until production):\n\n" + "\n".join(failures))
