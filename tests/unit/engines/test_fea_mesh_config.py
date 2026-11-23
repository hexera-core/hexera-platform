# Responsibility: Prove the production FEA gmsh options extracted into `fea_mesh_config` are exactly
#                 what `fea_adapter` set before the extraction, and that the adapter now takes them
#                 from there.
# Boundaries: hermetic - a recording stand-in for gmsh; real gmsh is the native tier.
from __future__ import annotations

import pytest

from meshpipeline.engines import fea_mesh_config
from meshpipeline.engines.fea_plan import base_size
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
