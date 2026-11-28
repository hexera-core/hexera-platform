# Responsibility: The gmsh options Hexera's production FEA mesh runs with, as data.
# Owns: `fixed_options` (the run-independent part, which `gmsh/fea_adapter` applies), `options` (that
#       plus the diagonal-dependent size and the fill/fallback settings), `algorithm_switch_on_failure`
#       and the constants (`HXT_ALGORITHM_3D`, `LC_INTEGRATION_PRECISION`).
# Boundaries: pure - no gmsh, no I/O. The plan's zones, size fields, thread count and 3D-algorithm
#             override stay with the adapter; this is only what every production run sets the same way.
# Collaborates with: engines/gmsh/fea_adapter.py (applies `fixed_options`), engines/fea_plan.py
#                    (`base_size`), cad/repair_v4/verdict (the panel's production member).
"""Production FEA gmsh options, extracted from the adapter so a second consumer can run them.

The CRS v4 checker meshes every repaired file with these same options (its `GmshProd` member), so
"meshes for Hexera's FEA" is judged by the settings that really deliver a mesh, not by a copy that
drifts. What is shared with the adapter, value for value:

- `fixed_options()`: the adapter applies it (`fea_adapter.apply_global_options`).
- `HXT_ALGORITHM_3D`: the adapter's `ALGORITHMS_3D["hxt"]` IS this constant.
- `algorithm_switch_on_failure`: the adapter calls it where it sets `Mesh.AlgorithmSwitchOnFailure`.
- the base element size: both call `fea_plan.base_size`; the adapter reads it back out of the plan's
  `base_size_m` (rounded to 6 figures by `fea_plan.finish`).

What is NOT shared, and can drift without moving `options()`: the plan's zones, size fields, thread
counts, the Delaunay fallback pass and a plan's own `base_size_m` when a user's element count or
sizes change it (`scale`, `beyond`). `options()` is the DEFAULT plan only. `tests/unit/engines/test_fea_mesh_config.py` holds the
four shared points to the adapter; nothing holds the unshared ones.
"""

from __future__ import annotations

from meshpipeline.engines.fea_plan import base_size

#: How exactly gmsh integrates 1/size along a curve to place its nodes (its default is 1e-9). THE CURVES
#: MESHED IN SECONDS, NOT MINUTES (fc_folding_chair, 24 Oct: 979 s of a 1,064 s pass in 1D): gmsh
#: integrates 1/size along every curve evaluating the whole size field each step; 1e-3 places the same
#: nodes to a tenth of a percent (97,319 vs 97,484 nodes after 2D) in 26 s.
LC_INTEGRATION_PRECISION = 1e-3

#: Mesh.MeshSizeFromCurvature, elements per 2*pi of a curved face (0 = off). THE ONE VALUE the product mesher and the
#: CRS clean check's gmsh members share (owner, 2026-11-27: check and product mesher stay matched;
#: cad/repair_v4/verdict/thin_sizing reads it from here).
CURVATURE_POINTS = 0

#: gmsh's Mesh.Algorithm3D number for HXT, the production fill (`fea_adapter.DEFAULT_ALGORITHM_3D`).
HXT_ALGORITHM_3D = 10


def algorithm_switch_on_failure(surface_switch: bool | None = None) -> int:
    """`Mesh.AlgorithmSwitchOnFailure`: 1 (gmsh retries a stalled surface with another 2D algorithm)
    unless the run turned the switch off (`surface_switch` is False). The adapter sets it from here."""
    return 0 if surface_switch is False else 1


def fixed_options() -> dict[str, float | int]:
    """The options every production run sets, whatever the plan: a fixed seed, the curves and
    surfaces on one thread (their parallel versions are not repeatable), the curve integration
    precision, and no sizing from curvature, points or the boundary - the plan alone sizes the mesh.
    """
    return {
        "Mesh.MaxNumThreads1D": 1,
        "Mesh.MaxNumThreads2D": 1,
        "Mesh.RandomSeed": 1,
        "Mesh.LcIntegrationPrecision": LC_INTEGRATION_PRECISION,
        "Geometry.OCCBoundsUseStl": 1,
        "Mesh.MeshSizeMin": 0.0,
        "Mesh.MeshSizeFromCurvature": CURVATURE_POINTS,
        "Mesh.MeshSizeFromPoints": 0,
        "Mesh.MeshSizeExtendFromBoundary": 0,
    }


def options(diag_m: float) -> dict[str, float | int]:
    """Every option of a default production pass over a part whose diagonal is `diag_m` metres: the
    fixed ones, HXT as the 3D fill, the base element size (a tenth of the part, `base_size`) and the
    switch to another 2D algorithm when one stalls. A plan's zone sizes and overrides refine this
    in the adapter; they are not part of it.
    """
    return {
        **fixed_options(),
        "Mesh.Algorithm3D": HXT_ALGORITHM_3D,
        "Mesh.MeshSizeMax": base_size(diag_m),
        "Mesh.AlgorithmSwitchOnFailure": algorithm_switch_on_failure(),
    }
