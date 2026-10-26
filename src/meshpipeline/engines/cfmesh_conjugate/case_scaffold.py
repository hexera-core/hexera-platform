# Responsibility: Lay out the OpenFOAM case directory a conjugate run meshes into.
# Boundaries: structure only. The mesher writes meshDict, the two region meshes and regionProperties.
from __future__ import annotations

from pathlib import Path

#: ASCII, on purpose. The delivered mesh is read back face by face - the per-region boundary
#: files, the surface the reviewer opens, the viewer's own surfaces - and a binary polyMesh makes
#: every one of those readers fall back or fail.
_HEADER = ("FoamFile\n{{\n    version 2.0; format ascii; class dictionary;\n"
           "    location system; object {name};\n}}\n")


def write_case_skeleton(workspace) -> None:
    """The system/ dictionaries every OpenFOAM utility in the chain expects to find.

    cartesianMesh, topoSet, splitMeshRegions, checkMesh and foamToVTK are all OpenFOAM
    applications and every one of them reads controlDict before doing anything. A conjugate run
    is five of those in a row, so the case needs this before the first one starts.
    """
    system = Path(workspace) / "system"
    system.mkdir(parents=True, exist_ok=True)
    (system / "controlDict").write_text(
        _HEADER.format(name="controlDict")
        + "application chtMultiRegionFoam;\n"
        "startFrom startTime; startTime 0; stopAt endTime; endTime 1;\n"
        "deltaT 1; writeControl timeStep; writeInterval 1;\n"
        "writeFormat ascii; writeCompression off;\n"
    )
    (system / "fvSchemes").write_text(
        _HEADER.format(name="fvSchemes")
        + "ddtSchemes { default steadyState; }\n"
        "gradSchemes { default Gauss linear; }\n"
        "divSchemes { default none; }\n"
        "laplacianSchemes { default Gauss linear limited corrected 0.333; }\n"
        "interpolationSchemes { default linear; }\n"
        "snGradSchemes { default limited corrected 0.333; }\n"
    )
    (system / "fvSolution").write_text(
        _HEADER.format(name="fvSolution")
        + "solvers {}\nSIMPLE { nNonOrthogonalCorrectors 0; }\n"
    )


__all__ = ["write_case_skeleton"]
