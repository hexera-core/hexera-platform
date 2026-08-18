# Responsibility: Verify each layer imports only downwards, with no deferred import hiding the edge.
from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "src" / "meshpipeline"

#: layer -> layers it may NOT import
FORBIDDEN = {
    "engines": ("application",),
    "contracts": ("application", "agents", "api", "engines", "persistence", "pipeline", "runtime"),
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
    return out


def _modules(layer: str):
    return sorted((SRC / layer).rglob("*.py"))


@pytest.mark.parametrize("layer", sorted(FORBIDDEN))
def test_a_layer_never_imports_the_layers_above_it(layer):
    violations = []
    for path in _modules(layer):
        for mod in _imports(path):
            for banned in FORBIDDEN[layer]:
                if mod == f"meshpipeline.{banned}" or mod.startswith(f"meshpipeline.{banned}."):
                    violations.append(f"{path.relative_to(SRC)} -> {mod}")
    assert not violations, (
        f"{layer}/ imports a layer it must not depend on:\n  " + "\n  ".join(violations))


def test_the_registry_direction_still_runs_application_to_engine():
    registry = _imports(SRC / "engines" / "registry.py")
    assert not any(m.startswith("meshpipeline.application") for m in registry)
    app_side = _imports(SRC / "application" / "pipeline_run.py")
    assert any(m.startswith("meshpipeline.engines") for m in app_side), (
        "the application no longer selects engines - the dependency was inverted the other way")


def test_no_circular_import_workaround_was_introduced():
    offenders = []
    for path in _modules("engines"):
        src = path.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(src.splitlines(), 1):
            if line.strip().startswith(("from meshpipeline.application", "import meshpipeline.application")):
                offenders.append(f"{path.relative_to(SRC)}:{i}")
    assert not offenders, (
        "engines/ reaches into application/ at call time - a deferred import is the same edge: "
        + ", ".join(offenders))


def test_the_fencing_contract_is_usable_without_the_application():
    from meshpipeline.contracts import execution_guard

    assert hasattr(execution_guard, "StaleWorkerFenced")
    assert hasattr(execution_guard, "assert_still_owner")
    src = (SRC / "contracts" / "execution_guard.py").read_text()
    assert "meshpipeline.application" not in src
    assert "meshpipeline.persistence" not in src


def test_the_application_and_the_contract_share_one_fenced_exception():
    from meshpipeline.application import execution_fence
    from meshpipeline.contracts import execution_guard

    assert execution_fence.StaleWorkerFenced is execution_guard.StaleWorkerFenced


async def test_an_unfenced_run_is_a_no_op_not_a_refusal():
    from meshpipeline.contracts import execution_guard

    assert await execution_guard.still_owner() is True
    await execution_guard.assert_still_owner("nothing is fencing this")


async def test_an_installed_checker_fences_the_worker():
    from meshpipeline.contracts import execution_guard

    async def _lost():
        return False

    with execution_guard.owner_checker(_lost):
        assert await execution_guard.still_owner() is False
        with pytest.raises(execution_guard.StaleWorkerFenced):
            await execution_guard.assert_still_owner("native mesh step")
    # ...and the installation is scoped
    assert await execution_guard.still_owner() is True
