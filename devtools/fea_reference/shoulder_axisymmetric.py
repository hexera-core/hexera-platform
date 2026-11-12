# Responsibility: Reproduce the stepped shaft's reference stress concentration (Kt) used by the FEA accuracy suite: a fine 2D axisymmetric CalculiX model of the shoulder fillet, independent of the product's own meshing path, refined at the fillet until it converges.
# Boundaries: a developer tool; nothing in the product imports it. Needs gmsh (the pinned wheel) and a CalculiX executable (HEXERA_CCX, or ccx on PATH).
# Collaborates with: tests/unit/engines/test_fea_textbook.py (SHAFT["kt"] is this script's converged answer).
"""THE SHOULDER FILLET REFERENCE, BY A CONVERGED AXISYMMETRIC MODEL.

The textbook suite's stepped shaft, as drawn there: d = 40 mm for the first 80 mm, D = 60 mm for the next 80 mm,
the shoulder filleted r = 5 mm, the shoulder's outer edge and both ends rounded 2 mm. Axisymmetric (CAX8, CAX6 where
gmsh leaves a triangle), the fillet meshed at the size given, 2 mm far from it. The bottom end is held axially, the
flat of the top end pulled by 1 MPa; Kt is the largest axial stress over the small section's mean axial stress (the
top flat's force over the d section: 1 MPa x ((D/2 - 2) / (d/2))^2), as the 3D case reads it.

    python devtools/fea_reference/shoulder_axisymmetric.py 0.2 0.1 0.05

The Pilkey fit for a shoulder fillet in tension (Peterson's chart 3.4) is printed beside it.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

D, d, RF, L, RR, NU = 60.0, 40.0, 5.0, 160.0, 2.0, 0.3
HALF = L / 2


def pilkey_fit() -> float:
    """Kt of a shoulder fillet in a round bar in tension (Pilkey, Peterson's chart 3.4; 0.1 <= h/r <= 2)."""
    h = (D - d) / 2
    q = h / RF
    s = math.sqrt(q)
    c1 = 0.926 + 1.157 * s - 0.099 * q
    c2 = 0.012 - 3.036 * s + 0.961 * q
    c3 = -0.302 + 3.977 * s - 1.744 * q
    c4 = 0.365 - 2.098 * s + 0.878 * q
    x = 2 * h / D
    return c1 + c2 * x + c3 * x ** 2 + c4 * x ** 3


def _mesh(fillet_size: float):
    import gmsh

    a, b = d / 2, D / 2
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        g = gmsh.model.geo
        P = {}
        for k, (x, y) in {"o": (0, 0), "b0": (a - RR, 0), "bc": (a - RR, RR), "b1": (a, RR),
                          "f0": (a, HALF - RF), "fc": (a + RF, HALF - RF), "f1": (a + RF, HALF),
                          "s0": (b - RR, HALF), "sc": (b - RR, HALF + RR), "s1": (b, HALF + RR),
                          "t0": (b, L - RR), "tc": (b - RR, L - RR), "t1": (b - RR, L), "top": (0, L)}.items():
            P[k] = g.addPoint(x, y, 0)
        cur = [g.addLine(P["o"], P["b0"]), g.addCircleArc(P["b0"], P["bc"], P["b1"]), g.addLine(P["b1"], P["f0"]),
               g.addCircleArc(P["f0"], P["fc"], P["f1"]), g.addLine(P["f1"], P["s0"]),
               g.addCircleArc(P["s0"], P["sc"], P["s1"]), g.addLine(P["s1"], P["t0"]),
               g.addCircleArc(P["t0"], P["tc"], P["t1"]), g.addLine(P["t1"], P["top"]), g.addLine(P["top"], P["o"])]
        g.addPlaneSurface([g.addCurveLoop(cur)])
        g.synchronize()
        fillet = cur[3]
        f = gmsh.model.mesh.field.add("Distance")
        gmsh.model.mesh.field.setNumbers(f, "CurvesList", [fillet])
        gmsh.model.mesh.field.setNumber(f, "Sampling", 400)
        t = gmsh.model.mesh.field.add("Threshold")
        gmsh.model.mesh.field.setNumber(t, "InField", f)
        gmsh.model.mesh.field.setNumber(t, "SizeMin", fillet_size)
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
        at = {int(i): (float(x), float(y)) for i, (x, y, _z) in zip(tags, xyz, strict=True)}
        lines = ["*NODE"] + [f"{i}, {x:.10g}, {y:.10g}" for i, (x, y) in at.items()]
        sets, loads = [], []
        etypes, etags, enodes = gmsh.model.mesh.getElements(2)
        for typ, et, en in zip(etypes, etags, enodes, strict=True):
            nn = gmsh.model.mesh.getElementProperties(typ)[3]
            corners = 4 if nn == 8 else 3
            sets.append(f"E{nn}")
            lines.append(f"*ELEMENT, TYPE={'CAX8' if nn == 8 else 'CAX6'}, ELSET=E{nn}")
            for e, row in zip(et, en.reshape(-1, nn), strict=True):
                lines.append(f"{int(e)}, " + ", ".join(str(int(v)) for v in row))
                for k in range(corners):
                    p, q = at[int(row[k])], at[int(row[(k + 1) % corners])]
                    if abs(p[1] - L) < 1e-9 and abs(q[1] - L) < 1e-9:
                        loads.append(f"{int(e)}, P{k + 1}, -1.0")
    finally:
        gmsh.finalize()
    lines += ["*NSET, NSET=BOT"] + [str(i) for i, (x, y) in at.items() if abs(y) < 1e-9]
    lines += ["*ELSET, ELSET=ALL", *sets]
    return lines, loads, at


def _block(frd: str, name: str, cols: int) -> dict[int, list[float]]:
    out = {}
    for ln in frd.split(name, 1)[1].splitlines()[1:]:
        if ln.startswith(" -3"):
            break
        if ln.startswith(" -1"):
            out[int(ln[3:13])] = [float(ln[13 + 12 * k:25 + 12 * k]) for k in range(cols)]
    return out


def kt(fillet_size: float, ccx: str) -> float:
    lines, loads, at = _mesh(fillet_size)
    lines += ["*MATERIAL, NAME=S", "*ELASTIC", f"210000, {NU}", "*SOLID SECTION, ELSET=ALL, MATERIAL=S",
              "*BOUNDARY", "BOT, 2, 2, 0.0", "*STEP", "*STATIC", "*DLOAD", *loads, "*NODE FILE", "S", "*END STEP"]
    work = Path(tempfile.mkdtemp(prefix="shoulder_axi_"))
    (work / "axi.inp").write_text("\n".join(lines) + "\n")
    subprocess.run([ccx, "axi"], cwd=work, capture_output=True, text=True, check=True)
    stress = _block((work / "axi.frd").read_text(), "STRESS", 2)
    shutil.rmtree(work, ignore_errors=True)
    near = [s[1] for i, s in stress.items() if abs(at[i][1] - HALF) < 3 * RF]
    nominal = 1.0 * ((D / 2 - RR) / (d / 2)) ** 2
    return max(near) / nominal


if __name__ == "__main__":
    solver = os.environ.get("HEXERA_CCX") or shutil.which("ccx") or ""
    if not solver:
        sys.exit("CalculiX (ccx) not found: set HEXERA_CCX")
    print(f"Pilkey fit (Peterson chart 3.4): Kt = {pilkey_fit():.4f}")
    for size in [float(v) for v in sys.argv[1:]] or [0.2, 0.1, 0.05]:
        print(f"fillet element {size:g} mm: Kt = {kt(size, solver):.4f}")
