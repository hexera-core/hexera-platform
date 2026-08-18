# Responsibility: Verify the product's layers stay neutral - no adapter, runtime or path mutation out of place.
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).parents[3]
SRC = REPO / "src" / "meshpipeline"

PRODUCT = {"agents", "engines", "pipeline", "cad", "render", "sandbox", "capture", "agent_tools"}


def _files(*pkgs: str) -> list[Path]:
    roots = [SRC / p for p in pkgs] if pkgs else [SRC]
    return [p for root in roots for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _meshpipeline_subpackages(path: Path) -> set[str]:
    mods: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("meshpipeline."):
            parts = node.module.split(".")
            if len(parts) > 1:
                mods.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("meshpipeline."):
                    parts = alias.name.split(".")
                    if len(parts) > 1:
                        mods.add(parts[1])
    return mods


def test_contracts_are_neutral():
    forbidden = {"adapters", "application", "api", "runtime", "persistence"} | PRODUCT
    bad = {}
    for p in _files("contracts"):
        up = _meshpipeline_subpackages(p) & forbidden
        if up:
            bad[p.relative_to(SRC).as_posix()] = sorted(up)
    assert not bad, f"contracts/ must be neutral (imports something above itself): {bad}"


def test_product_and_application_import_no_concrete_adapters():
    bad = {}
    for p in _files(*PRODUCT, "application"):
        if "adapters" in _meshpipeline_subpackages(p):
            hits = sorted(
                line.strip()
                for line in p.read_text().splitlines()
                if "meshpipeline.adapters" in line and ("import" in line)
            )
            bad[p.relative_to(SRC).as_posix()] = hits
    assert not bad, (
        "product/application must not import concrete adapters - use a neutral contract with "
        f"runtime-injected DI: {bad}")


def test_api_is_adapter_neutral():
    bad = sorted(
        p.relative_to(SRC).as_posix()
        for p in _files("api")
        if "adapters" in _meshpipeline_subpackages(p)
    )
    assert not bad, f"api/ must be adapter-neutral (runtime composes adapters), but these import one: {bad}"


def test_backend_selection_lives_only_in_runtime_composition():
    offenders = [
        p.relative_to(SRC).as_posix()
        for p in _files()
        if p.relative_to(SRC).parts[0] != "runtime" and "build_pipeline_launcher" in p.read_text()
    ]
    assert not offenders, f"only runtime composition may select a pipeline backend: {offenders}"


def test_product_package_never_imports_deploy():
    offenders = [
        p.relative_to(SRC).as_posix()
        for p in _files()
        if "import deploy" in (t := p.read_text()) or "from deploy" in t
    ]
    assert not offenders, f"src/meshpipeline must not import deploy/: {offenders}"


def test_adapters_never_import_runtime():
    offenders = {
        p.relative_to(SRC).as_posix(): sorted(_meshpipeline_subpackages(p) & {"runtime"})
        for p in _files("adapters")
        if "runtime" in _meshpipeline_subpackages(p)
    }
    assert not offenders, f"adapters/ must not import runtime/: {offenders}"


def test_capture_never_imports_the_things_it_observes():
    forbidden = {"agents", "engines", "pipeline"}
    offenders = {
        p.relative_to(SRC).as_posix(): sorted(_meshpipeline_subpackages(p) & forbidden)
        for p in _files("capture")
        if _meshpipeline_subpackages(p) & forbidden
    }
    assert not offenders, (
        f"capture/ must not import what it observes: {offenders}")


def test_pipeline_nodes_never_import_the_graph_composition_root():
    offenders = []
    for p in _files("pipeline"):
        if p.name == "graph.py":
            continue
        src = p.read_text()
        if "pipeline.graph" in src or "from meshpipeline.pipeline import graph" in src:
            offenders.append(p.relative_to(SRC).as_posix())
    assert not offenders, (
        f"a pipeline node imports the graph composition root: {offenders}")


def test_settings_imports_nothing_above_itself():
    forbidden = {"agents", "engines", "pipeline", "api", "application", "adapters",
                 "runtime", "capture", "cad", "render", "sandbox", "agent_tools",
                 "persistence"}
    offenders = {
        p.relative_to(SRC).as_posix(): sorted(_meshpipeline_subpackages(p) & forbidden)
        for p in _files("settings")
        if _meshpipeline_subpackages(p) & forbidden
    }
    assert not offenders, f"settings/ must not import upward: {offenders}"


def test_shipped_code_contains_no_sys_path_mutation():
    offenders = []
    for p in _files():
        for node in ast.walk(ast.parse(p.read_text())):
            if not isinstance(node, ast.Attribute) or node.attr not in ("insert", "append", "extend"):
                continue
            tgt = node.value
            if (isinstance(tgt, ast.Attribute) and tgt.attr == "path"
                    and isinstance(tgt.value, ast.Name) and tgt.value.id == "sys"):
                offenders.append(f"{p.relative_to(SRC).as_posix()}:{node.lineno}")
    assert not offenders, (
        f"shipped code mutates sys.path - imports must come from the installed package: {offenders}")
