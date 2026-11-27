# Responsibility: Prove the production FEA gmsh options extracted into `fea_mesh_config` are exactly
#                 what `fea_adapter` set before the extraction, and that the adapter now takes them
#                 from there.
# Boundaries: hermetic - a recording stand-in for gmsh; real gmsh is the native tier.
from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest

from meshpipeline.engines import fea_mesh_config
from meshpipeline.engines import fea_plan as P
from meshpipeline.engines.fea_plan import base_size
from meshpipeline.engines.fea_size_field import Histogram
from meshpipeline.engines.gmsh import fea_adapter

#: Every option the adapter's `mesh()` set globally, and `_build()` set for size and fallback, BEFORE
#: the extraction (fea_adapter.py as of a628d9b3: lines 1524, 1655 and 1977-2001), for a part whose
#: diagonal is 1.0 m, a plan with no overrides, one thread. Typed in from that source, not computed.
BEFORE_EXTRACTION_DIAG_1 = {
    "Mesh.MaxNumThreads1D": 1,
    "Mesh.MaxNumThreads2D": 1,
    "Mesh.RandomSeed": 1,
    "Mesh.LcIntegrationPrecision": 1e-3,
    "Geometry.OCCBoundsUseStl": 1,
    "Mesh.MeshSizeMin": 0.0,
    "Mesh.MeshSizeFromCurvature": 0,
    "Mesh.MeshSizeFromPoints": 0,
    "Mesh.MeshSizeExtendFromBoundary": 0,
    "Mesh.Algorithm3D": 10,  # DEFAULT_ALGORITHM_3D == "hxt"
    "Mesh.MeshSizeMax": 0.1,  # base_size(1.0): a tenth of the part
    "Mesh.AlgorithmSwitchOnFailure": 1,
}


class _Recorder:
    """Stands in for `gmsh.option`: records every setNumber, last write wins."""

    def __init__(self) -> None:
        self.set: dict[str, float] = {}
        self.calls: list[tuple[str, float]] = []

    def setNumber(self, key: str, value: float) -> None:  # noqa: N802 - gmsh's own name
        self.set[key] = value
        self.calls.append((key, value))


class _Gmsh:
    def __init__(self) -> None:
        self.option = _Recorder()


def test_fea_mesh_config_matches_adapter() -> None:
    assert fea_mesh_config.options(1.0) == BEFORE_EXTRACTION_DIAG_1


def test_adapter_global_block_sets_only_what_the_config_names() -> None:
    gmsh = _Gmsh()
    fea_adapter.apply_global_options(gmsh, {"threads": 1})
    expected = {
        k: v
        for k, v in BEFORE_EXTRACTION_DIAG_1.items()
        if k not in ("Mesh.MeshSizeMax", "Mesh.AlgorithmSwitchOnFailure")
    }
    # what the adapter set in that block before: the run's thread counts and General.NumThreads besides
    assert gmsh.option.set == {**expected, "General.NumThreads": 1, "Mesh.MaxNumThreads3D": 1}
    assert len(gmsh.option.calls) == len(gmsh.option.set)  # nothing set twice


def test_adapter_block_follows_the_plan_algorithm() -> None:
    gmsh = _Gmsh()
    fea_adapter.apply_global_options(gmsh, {"threads": 1, "algorithm3d": "delaunay"})
    assert gmsh.option.set["Mesh.Algorithm3D"] == 1


def test_config_algorithm_is_the_adapters_default() -> None:
    assert (
        fea_mesh_config.options(1.0)["Mesh.Algorithm3D"] == fea_adapter.ALGORITHMS_3D[fea_adapter.DEFAULT_ALGORITHM_3D]
    )


@pytest.mark.parametrize("diag", [0.05, 0.3737, 1.0, 12.5])
def test_mesh_size_is_the_plans_base_size(diag: float) -> None:
    assert fea_mesh_config.options(diag)["Mesh.MeshSizeMax"] == pytest.approx(base_size(diag))


def test_options_do_not_share_state() -> None:
    first = fea_mesh_config.options(1.0)
    first["Mesh.RandomSeed"] = 99
    assert fea_mesh_config.options(1.0)["Mesh.RandomSeed"] == 1


def test_adapter_switch_comes_from_the_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mesh.AlgorithmSwitchOnFailure: the adapter's setter reads `fea_mesh_config`, so a change there moves both."""
    gmsh = _Gmsh()
    fea_adapter.apply_surface_switch(gmsh, {})
    assert gmsh.option.set == {"Mesh.AlgorithmSwitchOnFailure": fea_mesh_config.options(1.0)["Mesh.AlgorithmSwitchOnFailure"]}
    off = _Gmsh()
    fea_adapter.apply_surface_switch(off, {"surface_switch": False})
    assert off.option.set == {"Mesh.AlgorithmSwitchOnFailure": 0}
    monkeypatch.setattr(fea_mesh_config, "algorithm_switch_on_failure", lambda _s=None: 7)
    moved = _Gmsh()
    fea_adapter.apply_surface_switch(moved, {})
    assert moved.option.set == {"Mesh.AlgorithmSwitchOnFailure": 7}
    assert fea_mesh_config.options(1.0)["Mesh.AlgorithmSwitchOnFailure"] == 7


def test_adapter_hxt_number_is_the_configs_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Load a second copy of the adapter against a config whose HXT number is different: its table follows."""
    monkeypatch.setattr(fea_mesh_config, "HXT_ALGORITHM_3D", 77)
    path = Path(fea_adapter.__file__)
    spec = importlib.util.spec_from_file_location("fea_adapter_probe", path)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert probe.ALGORITHMS_3D["hxt"] == 77
    assert fea_adapter.ALGORITHMS_3D["hxt"] == 10  # the real table is untouched


def test_plans_base_size_is_the_configs_base_size() -> None:
    """The adapter sets Mesh.MeshSizeMax from the plan's `base_size_m`; for the default plan that is
    `fea_plan.base_size`, the very function `options()` calls (the plan rounds it to six figures)."""
    record = {"bodies": 1, "faces": {}, "hot_spots": []}
    hist = Histogram(rows=[(math.inf, 1e-6)])
    for diag in (0.05, 0.2236, 1.0, 3.3):
        plan = P.finish(record, [], hist, diag_m=diag, limit=P.ELEMENT_LIMIT)
        assert plan["base_size_m"] == pytest.approx(fea_mesh_config.options(diag)["Mesh.MeshSizeMax"], rel=1e-5)


def test_the_adapter_build_calls_the_setter_and_spells_no_literal() -> None:
    """`test_adapter_switch_comes_from_the_config` calls `apply_surface_switch` directly, so it cannot see
    the call site. Read the source: some function other than the setter calls it, and `Mesh.AlgorithmSwitchOnFailure`
    is named nowhere else in the adapter (a literal at the call site would bypass the config)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(fea_adapter))
    callers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef) and fn.name != "apply_surface_switch"
        for call in ast.walk(fn)
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "apply_surface_switch"
    }
    assert "_build" in callers, callers
    spellers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and node.value == "Mesh.AlgorithmSwitchOnFailure"
    }
    assert spellers == {"apply_surface_switch"}, spellers
