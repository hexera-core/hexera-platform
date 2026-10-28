# Responsibility: Reproduce the U-groove's reference stress concentration (Kt) used by the FEA accuracy suite: a fine 2D axisymmetric CalculiX model, independent of the product's own meshing path, refined at the groove root until it converges.
# Boundaries: a developer tool; nothing in the product imports it. Needs gmsh (the pinned wheel) and a CalculiX executable (HEXERA_CCX, or ccx on PATH).
# Collaborates with: tests/unit/engines/test_fea_textbook.py (GROOVE["kt"] is this script's converged answer).
"""THE U-GROOVE REFERENCE, BY A CONVERGED AXISYMMETRIC MODEL.

A round bar D = 50 mm with a U-groove of radius 5 mm at mid-length (root diameter 40 mm), 200 mm
long, steel nu = 0.3. Half the length is modelled (symmetry at the groove plane), axisymmetric
(CAX8 quadrilaterals, CAX6 where gmsh leaves a triangle), the groove arc meshed at the root size
given, 2 mm far from it. The bar is pulled by a uniform displacement of its far end; Kt is the
largest axial stress over the net-section stress, the net-section load taken from the far-field
stress (sigma_far * (D/d)^2), so no reaction convention enters.

    python devtools/fea_reference/groove_axisymmetric.py 0.2 0.1 0.05 0.025

Converged (2026-10-21, CalculiX 2.21): Kt = 2.1947, 2.1937, 2.1935, 2.1934 -> 2.193. The published
fits (Pilkey/Roark 2.06, Neuber's formula 2.05) are about 6% lower.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

R, GROOVE_R, HALF_L, NU = 25.0, 5.0, 100.0, 0.3


def _mesh(root_size: float) -> tuple[list[str], dict[int, tuple[float, float]]]:
    import gmsh

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        occ = gmsh.model.occ
        rect = occ.addRectangle(0, 0, 0, R, HALF_L)
        disk = occ.addDisk(R, 0, 0, GROOVE_R, GROOVE_R)
        occ.cut([(2, rect)], [(2, disk)])
        occ.synchronize()
        arc = [c for _d, c in gmsh.model.getEntities(1) if gmsh.model.getType(1, c) != "Line"]
        f = gmsh.model.mesh.field.add("Distance")
        gmsh.model.mesh.field.setNumbers(f, "CurvesList", arc)
        gmsh.model.mesh.field.setNumber(f, "Sampling", 400)
        t = gmsh.model.mesh.field.add("Threshold")
        gmsh.model.mesh.field.setNumber(t, "InField", f)
        gmsh.model.mesh.field.setNumber(t, "SizeMin", root_size)
        gmsh.model.mesh.field.setNumber(t, "SizeMax", 2.0)
        gmsh.model.mesh.field.setNumber(t, "DistMin", 1.0)
        gmsh.model.mesh.field.setNumber(t, "DistMax", 15.0)
        gmsh.model.mesh.field.setAsBackgroundMesh(t)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.RecombineAll", 1)
        gmsh.option.setNumber("Mesh.Algorithm", 8)
        gmsh.model.mesh.generate(2)
        gmsh.option.setNumber("Mesh.SecondOrderIncomplete", 1)
        gmsh.model.mesh.setOrder(2)
        tags, coords, _ = gmsh.model.mesh.getNodes()
        xyz = coords.reshape(-1, 3)
        lines = ["*NODE"] + [f"{int(i)}, {x:.10g}, {y:.10g}" for i, (x, y, _z) in zip(tags, xyz, strict=True)]
        sets = []
        etypes, etags, enodes = gmsh.model.mesh.getElements(2)
        for typ, et, en in zip(etypes, etags, enodes, strict=True):
            nn = gmsh.model.mesh.getElementProperties(typ)[3]
            sets.append(f"E{nn}")
            lines.append(f"*ELEMENT, TYPE={'CAX8' if nn == 8 else 'CAX6'}, ELSET=E{nn}")
            lines += [f"{int(e)}, " + ", ".join(str(int(v)) for v in row)
                      for e, row in zip(et, en.reshape(-1, nn), strict=True)]
    finally:
        gmsh.finalize()
    at = {int(i): (float(x), float(y)) for i, (x, y, _z) in zip(tags, xyz, strict=True)}
    lines += ["*NSET, NSET=BOT"] + [str(i) for i, (_x, y) in at.items() if abs(y) < 1e-9]
    lines += ["*NSET, NSET=TOP"] + [str(i) for i, (_x, y) in at.items() if abs(y - HALF_L) < 1e-9]
    lines += ["*ELSET, ELSET=ALL", *sets]
    return lines, at


def _block(frd: str, name: str, cols: int) -> dict[int, list[float]]:
    out = {}
    for ln in frd.split(name, 1)[1].splitlines()[1:]:
        if ln.startswith(" -3"):
            break
        if ln.startswith(" -1"):
            out[int(ln[3:13])] = [float(ln[13 + 12 * k:25 + 12 * k]) for k in range(cols)]
    return out


def kt(root_size: float, ccx: str) -> float:
    """The net-section Kt with the groove root meshed at `root_size` mm."""
    lines, _at = _mesh(root_size)
    lines += ["*MATERIAL, NAME=S", "*ELASTIC", f"210000, {NU}", "*SOLID SECTION, ELSET=ALL, MATERIAL=S",
              "*BOUNDARY", "BOT, 2, 2, 0.0", "*STEP", "*STATIC", "*BOUNDARY", "TOP, 2, 2, 0.01",
              "*NODE FILE", "S", "*END STEP"]
    work = Path(tempfile.mkdtemp(prefix="groove_axi_"))
    try:
        (work / "axi.inp").write_text("\n".join(lines) + "\n")
        subprocess.run([ccx, "axi"], cwd=work, capture_output=True, text=True, check=True)
        frd = (work / "axi.frd").read_text()
    finally:
        pass
    pos, stress = _block(frd, "    2C", 2), _block(frd, "STRESS", 2)
    shutil.rmtree(work, ignore_errors=True)
    far = [stress[i][1] for i, (x, y) in pos.items() if i in stress and y > HALF_L - 5 and x <= R + 1e-6]
    nominal = sum(far) / len(far) * (R / (R - GROOVE_R)) ** 2
    return max(s[1] for s in stress.values()) / nominal


if __name__ == "__main__":
    solver = os.environ.get("HEXERA_CCX") or shutil.which("ccx") or ""
    if not solver:
        sys.exit("CalculiX (ccx) not found: set HEXERA_CCX")
    for size in [float(a) for a in sys.argv[1:]] or [0.2, 0.1, 0.05]:
        print(f"root element {size:g} mm: Kt = {kt(size, solver):.4f}")
