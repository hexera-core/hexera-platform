# Responsibility: Lay out the OpenFOAM case directory a multi-region snappyHexMesh run is authored into.
# Boundaries: structure only.
from __future__ import annotations

from pathlib import Path


def write_case_skeleton(workspace) -> None:
    system_dir = Path(workspace) / "system"
    system_dir.mkdir(exist_ok=True)

    (system_dir / "controlDict").write_text(
        "FoamFile\n{\n    version 2.0; format ascii; class dictionary;\n"
        "    location system; object controlDict;\n}\n"
        "application simpleFoam;\n"
        "startFrom startTime; startTime 0; stopAt endTime; endTime 1;\n"
        "deltaT 1; writeControl timeStep; writeInterval 1;\n"
        # Force ASCII polyMesh so the solvability gate can parse owner/neighbour
        # and assemble the FV pressure-Poisson operator (a binary mesh would make it
        # silently fall back to checkMesh-only).
        "writeFormat ascii; writeCompression off;\n"
    )
    (system_dir / "fvSchemes").write_text(
        "FoamFile\n{\n    version 2.0; format ascii; class dictionary;\n"
        "    location system; object fvSchemes;\n}\n"
        "ddtSchemes { default steadyState; }\n"
        "gradSchemes { default Gauss linear; }\n"
        "divSchemes { default none; }\n"
        "laplacianSchemes { default Gauss linear limited corrected 0.333; }\n"
        "interpolationSchemes { default linear; }\n"
        "snGradSchemes { default limited corrected 0.333; }\n"
    )
    (system_dir / "fvSolution").write_text(
        "FoamFile\n{\n    version 2.0; format ascii; class dictionary;\n"
        "    location system; object fvSolution;\n}\n"
        "solvers {}\n"
        "SIMPLE { nNonOrthogonalCorrectors 0; }\n"
    )
