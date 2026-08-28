# Responsibility: Declare gmsh's own tunables: the finite-volume quality bars and the optimizer ladder budget.
# Boundaries: declaration only. The formulas the bars judge live in fv_metrics.py; the
#   enforcement lives in gates.py (control plane) and driver.py (mesh image).
from __future__ import annotations

from meshpipeline.settings.env import ConfigurationError, optional_env

# FINITE-VOLUME QUALITY BARS for the gmsh path, in checkMesh's own conventions
# (OpenFOAM v2412 primitiveMeshTools - the formulas are vendored in fv_metrics.py from the
# calibrated quality-audit scorer, so the engine's gate and the fleet audit measure identically).
#
# 70 deg is checkMesh's SEVERE non-orthogonality line: the 2026-08 calibrated fleet audit found
# gmsh tet fluid domains delivered at 73.9 deg max while every snappy mesh respected its own
# 65 deg bar. 65 deg is the standard meshQualityControls maxNonOrtho (the warn/advisory bar and
# the optimizer ladder's target). Skewness bars are checkMesh's maxInternalSkewness /
# maxBoundarySkewness defaults.
GMSH_FV_NONORTHO_HARD: float = float(optional_env("GMSH_FV_NONORTHO_HARD", "70"))
GMSH_FV_NONORTHO_WARN: float = float(optional_env("GMSH_FV_NONORTHO_WARN", "65"))
GMSH_FV_SKEW_INTERNAL_HARD: float = float(optional_env("GMSH_FV_SKEW_INTERNAL_HARD", "4.0"))
GMSH_FV_SKEW_BOUNDARY_HARD: float = float(optional_env("GMSH_FV_SKEW_BOUNDARY_HARD", "20.0"))

if GMSH_FV_NONORTHO_WARN > GMSH_FV_NONORTHO_HARD:
    raise ConfigurationError(
        f"GMSH_FV_NONORTHO_WARN ({GMSH_FV_NONORTHO_WARN}) must not exceed "
        f"GMSH_FV_NONORTHO_HARD ({GMSH_FV_NONORTHO_HARD}): the advisory bar sits inside the "
        "hard bar, never beyond it")
for _name, _v in (("GMSH_FV_NONORTHO_HARD", GMSH_FV_NONORTHO_HARD),
                  ("GMSH_FV_SKEW_INTERNAL_HARD", GMSH_FV_SKEW_INTERNAL_HARD),
                  ("GMSH_FV_SKEW_BOUNDARY_HARD", GMSH_FV_SKEW_BOUNDARY_HARD)):
    if _v <= 0:
        raise ConfigurationError(f"{_name} must be positive, got {_v}")

# How many optimizer passes the driver's deterministic ladder may spend after 3D meshing
# before it stops and reports whatever the metrics then are (the gate still judges them).
GMSH_FV_OPTIMIZE_MAX_PASSES: int = int(optional_env("GMSH_FV_OPTIMIZE_MAX_PASSES", "5"))
if GMSH_FV_OPTIMIZE_MAX_PASSES < 0:
    raise ConfigurationError(
        f"GMSH_FV_OPTIMIZE_MAX_PASSES must be >= 0, got {GMSH_FV_OPTIMIZE_MAX_PASSES}")
