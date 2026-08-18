# Responsibility: Hold the snappyHexMesh mechanics both snappy-family engines share.
# Boundaries: shared parsing and dictionary knowledge; each bundle keeps its own region handling and deliverable.
# Collaborates with: engines/snappy/ and engines/snappy_multiregion/.
from __future__ import annotations

import re
from pathlib import Path

#: The FoamFile header every case dictionary opens with.
_HDR = ("FoamFile{{ version 2.0; format ascii; class {cls}; object {obj}; }}\n")


def _write_case_skeleton(ws: Path) -> None:
    sysd = ws / "system"
    sysd.mkdir(parents=True, exist_ok=True)
    (sysd / "controlDict").write_text(
        _HDR.format(cls="dictionary", obj="controlDict")
        + "application snappyHexMesh; startFrom startTime; startTime 0; stopAt endTime;\n"
        "endTime 1; deltaT 1; writeControl timeStep; writeInterval 1;\n")
    # fvSchemes/fvSolution: snappy/blockMesh don't read these, but they ship WITH the delivered
    # mesh as the numerical setup, so we write a ROBUST steady incompressible external-aero
    # template rather than empty stubs. The robustness matters: `limited corrected 0.33` on the
    # non-orthogonal + surface-normal-gradient terms plus `linearUpwind` convection and
    # nNonOrthogonalCorrectors makes the solve tolerant of the few localized high-skew/non-ortho
    # faces every hex+prism mesh has at a wing-body junction (the reason a handful of skewed faces
    # is production-grade, not a defect). The user still supplies 0/ fields + fluid properties.
    (sysd / "fvSchemes").write_text(
        _HDR.format(cls="dictionary", obj="fvSchemes") + """
ddtSchemes { default steadyState; }
gradSchemes { default cellLimited Gauss linear 1; }
divSchemes {
    default none;
    div(phi,U) bounded Gauss linearUpwind grad(U);
    div(phi,k) bounded Gauss upwind;
    div(phi,omega) bounded Gauss upwind;
    div(phi,epsilon) bounded Gauss upwind;
    div(phi,nuTilda) bounded Gauss upwind;
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
}
laplacianSchemes { default Gauss linear limited corrected 0.33; }
interpolationSchemes { default linear; }
snGradSchemes { default limited corrected 0.33; }
wallDist { method meshWave; }
""")
    (sysd / "fvSolution").write_text(
        _HDR.format(cls="dictionary", obj="fvSolution") + """
solvers {
    p { solver GAMG; smoother GaussSeidel; tolerance 1e-7; relTol 0.05; }
    "(U|k|omega|epsilon|nuTilda)" { solver smoothSolver; smoother GaussSeidel; tolerance 1e-8; relTol 0.1; }
}
SIMPLE {
    nNonOrthogonalCorrectors 2;
    consistent yes;
    residualControl { p 1e-4; U 1e-4; "(k|omega|epsilon|nuTilda)" 1e-4; }
}
relaxationFactors { equations { U 0.9; ".*" 0.7; } }
""")


_ADDED = re.compile(r"Added (\d+) out of (\d+) cells \(([\d.]+)%\)")
# final layer summary row (OpenFOAM v2412):
#   <patch>  <faces>  <layers target>  <layers mesh>  <thickness[m]>  <thickness[%]>
# the "layers mesh" column is an AVERAGE (e.g. 2.36), so it is a float, not an int.
_LAYER_ROW = re.compile(
    r"^\s*([A-Za-z_][\w]*)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+([\d.eE+-]+)\s+([\d.]+)\s*$")


def parse_layer_coverage(snappy_log: str) -> dict:
    out: dict = {"overall_pct": None, "per_patch": {}}
    if not snappy_log:
        return out
    # headline: the last 'Added N out of M cells (X%)' (after final relaxation)
    added = _ADDED.findall(snappy_log)
    if added:
        n, m, pct = added[-1]
        out["overall_pct"] = float(pct)
        out["cells_with_layers"] = int(n)
        out["cells_targeted"] = int(m)
    # per-patch summary table: between the 'faces ... layers ... thickness' header and the
    # 'Layer mesh' line. Sub-header / divider lines simply don't match the numeric row.
    in_table = False
    for ln in snappy_log.splitlines():
        if "faces" in ln and "layers" in ln and "thickness" in ln:
            in_table = True
            continue
        if in_table:
            if "Layer mesh" in ln or "Cells per refinement" in ln:
                break
            mrow = _LAYER_ROW.match(ln)
            if mrow:
                name, faces, tgt, got, thick_m, cov = mrow.groups()
                out["per_patch"][name] = {
                    "faces": int(faces), "layers_target": int(tgt), "layers": round(float(got), 2),
                    "thickness_m": float(thick_m), "coverage_pct": float(cov)}
    return out


__all__ = ["_write_case_skeleton", "parse_layer_coverage"]
