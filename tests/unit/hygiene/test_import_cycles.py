# Responsibility: Verify the modules that could form a cycle stay base modules importing neither side.
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parents[3] / "src"

ENGINES = ("cfmesh", "snappy", "snappy_multiregion", "gmsh", "vmtk")


def _imported_modules(dotted: str) -> set[str]:
    path = SRC / (dotted.replace(".", "/") + ".py")
    tree = ast.parse(path.read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names if a.name.startswith("meshpipeline"))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.startswith("meshpipeline"):
                out.add(node.module)                                  # from pkg.mod import X
                out.update(f"{node.module}.{a.name}" for a in node.names)  # from pkg import mod
    return out


def _imports(importer: str, target: str) -> bool:
    mods = _imported_modules(importer)
    return target in mods or any(m == target or m.startswith(target + ".") for m in mods)


# Dispatch_contract <-> pipeline_run, broken via the neutral dispatch_types seam
def test_dispatch_contract_does_not_import_pipeline_run():
    assert not _imports("meshpipeline.application.dispatch_contract",
                        "meshpipeline.application.pipeline_run"), (
        "dispatch_contract must read the run entry from application.dispatch_types.run_entry, "
        "never import pipeline_run - that reintroduces the dispatch cycle.")


def test_dispatch_types_is_a_base_module_importing_neither_side():
    mods = _imported_modules("meshpipeline.application.dispatch_types")
    for forbidden in ("meshpipeline.application.pipeline_run",
                      "meshpipeline.application.dispatch_contract"):
        assert not any(m == forbidden or m.startswith(forbidden + ".") for m in mods), (
            f"dispatch_types must import neither side of the seam; it imports {forbidden}")


def test_pipeline_run_still_registers_its_entry():
    assert _imports("meshpipeline.application.pipeline_run",
                    "meshpipeline.application.dispatch_types"), (
        "pipeline_run must register its run entry via dispatch_types.register_run_entry")


# Every engine's review_renderer <-> spec, broken via per-engine _shared.py
@pytest.mark.parametrize("engine", ENGINES)
def test_engine_review_renderer_does_not_import_its_spec(engine):
    rr = f"meshpipeline.engines.{engine}.review_renderer"
    spec = f"meshpipeline.engines.{engine}.spec"
    assert not _imports(rr, spec), (
        f"{engine}: review_renderer must read shared review-contract data from _shared, not spec - "
        "importing spec reintroduces the renderer/spec cycle.")


@pytest.mark.parametrize("engine", ENGINES)
def test_engine_shared_is_a_base_module(engine):
    mods = _imported_modules(f"meshpipeline.engines.{engine}._shared")
    for sibling in (f"meshpipeline.engines.{engine}.spec",
                    f"meshpipeline.engines.{engine}.review_renderer"):
        assert not any(m == sibling or m.startswith(sibling + ".") for m in mods), (
            f"{engine}/_shared must import neither spec nor review_renderer; it imports {sibling}")
