# Responsibility: Prove the delivered gmsh FEA deck solves to textbook answers in CalculiX, with loads and supports applied on its named faces.
"""TEXTBOOK ACCURACY OF THE DELIVERED DECK (local only: needs CalculiX).

Each part is meshed by the real driver exactly as a run would, and the deck it writes is handed to
CalculiX with only a material, a section and the boundary conditions appended - those name the
deck's own Face_k / group sets, so the faces are proven as well as the elements.

- cantilever 300 x 20 x 20 mm steel, clamped on one end face:
  first bending frequency vs Euler-Bernoulli (185.6 Hz), and tip deflection under an end load
  spread over the named end face vs beam theory (Timoshenko: bending plus shear);
- quarter of a plate with a central hole, d/W = 0.2, in tension on its named end face, symmetry
  on its named cut faces: net-section Kt vs Heywood, 2 + (1 - d/W)^3 = 2.512. Poisson's ratio 0
  makes the 3D answer the plane-stress one exactly, so the plate thickness cannot bias Kt.

CI has no CalculiX: the module skips. Locally point HEXERA_CCX at a ccx executable (or put ccx on
PATH), e.g. HEXERA_CCX=~/fea_audit/ccx/ccx.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

CCX = os.environ.get("HEXERA_CCX") or shutil.which("ccx") or ""
pytestmark = pytest.mark.skipif(not (CCX and Path(CCX).exists()),
                                reason="CalculiX (ccx) not installed - set HEXERA_CCX to run")

SRC = Path(__file__).parents[3] / "src"
E_STEEL, NU_STEEL, RHO_STEEL = 210e9, 0.3, 7850.0


def _mesh(ws: Path, build, pick, groups) -> dict:
    """Write the CAD built by ``build`` (metres) to geometry.step, run the driver on it with the
    named ``groups`` (``pick`` finds their surface tags on the STEP as the driver reads it) and
    return quality.json."""
    gmsh = pytest.importorskip("gmsh")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        build(gmsh)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
        gmsh.clear()
        gmsh.model.occ.importShapes(str(ws / "geometry.step"))
        gmsh.model.occ.synchronize()
        tags = pick(gmsh)
    finally:
        gmsh.finalize()
    assert all(tags[n] for n, _ in groups), tags
    spec = {"element_order": 2, "size": {"mode": "factor", "value": 0.04}, "optimize": True,
            "groups": [{"name": n, "role": r, "surface_tags": tags[n]} for n, r in groups],
            "default_group": "free"}
    (ws / "gmsh_spec.json").write_text(json.dumps(spec))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(SRC), *filter(None, [os.environ.get("PYTHONPATH")])])}
    proc = subprocess.run([sys.executable, "-m", "meshpipeline.engines.gmsh.driver", str(ws)],
                          env=env, capture_output=True, text=True, timeout=1800)
    assert proc.returncode == 0, proc.stderr[-1500:]
    q = json.loads((ws / "quality.json").read_text())
    print(f"{ws.name}: {q['cells']} elements, {q['nodes']} nodes, min SICN {q['min_sicn']}")
    return q


def _faces_at(gmsh, axis: int, value: float) -> list[int]:
    out = []
    for _, s in gmsh.model.getEntities(2):
        b = gmsh.model.getBoundingBox(2, s)
        if abs(b[axis] - value) < 1e-6 and abs(b[axis + 3] - value) < 1e-6:
            out.append(s)
    return out


def _solve(ws: Path, job: str, tail: str) -> str:
    (ws / f"{job}.inp").write_text((ws / "mesh.inp").read_text() + tail)
    proc = subprocess.run([CCX, job], cwd=ws, capture_output=True, text=True, timeout=3600,
                          env={**os.environ, "OMP_NUM_THREADS": "2"})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0 and "*ERROR" not in out, out[-1500:]
    return (ws / f"{job}.dat").read_text()


def _deck_sets(ws: Path):
    """(nodes {id: xyz}, elements {id: conn}, surfaces {name: [(element set, label)]},
    element sets {name: ids}, node sets {name: ids}) read back from mesh.inp."""
    nodes, elements, surfaces, elsets, nsets = {}, {}, {}, {}, {}
    key, name = "", ""
    for line in (ws / "mesh.inp").read_text().splitlines():
        if line.startswith("**"):
            continue
        if line.startswith("*"):
            key = line.split(",")[0].upper()
            opts = dict(re.findall(r"(\w+)=(\w+)", line))
            name = opts.get("NAME") or opts.get("ELSET") or opts.get("NSET") or ""
            if key == "*SURFACE":
                surfaces[name] = []
            continue
        f = [v.strip() for v in line.split(",") if v.strip()]
        if key == "*NODE":
            nodes[int(f[0])] = np.array([float(v) for v in f[1:4]])
        elif key == "*ELEMENT":
            elements[int(f[0])] = [int(v) for v in f[1:]]
        elif key == "*SURFACE":
            surfaces[name].append((f[0], int(f[1][1:])))
        elif key == "*ELSET":
            elsets.setdefault(name, []).extend(int(v) for v in f)
        elif key == "*NSET":
            nsets.setdefault(name, []).extend(int(v) for v in f)
    return nodes, elements, surfaces, elsets, nsets


def _consistent_loads(ws: Path, surface: str, traction: np.ndarray) -> dict[int, np.ndarray]:
    """Nodal forces of a uniform traction over a named surface of 6-node element faces: a flat
    quadratic triangle carries nothing at its corners and a third of its force at each midside
    node (the consistent load)."""
    from meshpipeline.engines.gmsh.abaqus_deck import face_nodes
    nodes, elements, surfaces, elsets, _ = _deck_sets(ws)
    force: dict[int, np.ndarray] = {}
    for elset, lab in surfaces[surface]:
        for e in elsets[elset]:
            fn = face_nodes(np.array(elements[e]), lab)
            a, b, c = (nodes[int(i)] for i in fn[:3])
            area = 0.5 * float(np.linalg.norm(np.cross(b - a, c - a)))
            for m in fn[3:]:
                force[int(m)] = force.get(int(m), np.zeros(3)) + traction * area / 3.0
    return force


# ------------------------------------------------------------------ cantilever

L, B = 0.3, 0.02


@pytest.fixture(scope="module")
def cantilever(tmp_path_factory):
    ws = tmp_path_factory.mktemp("cantilever")

    def build(gmsh):
        gmsh.model.occ.addBox(0, 0, 0, L, B, B)

    def pick(gmsh):
        return {"root": _faces_at(gmsh, 0, 0.0), "tip": _faces_at(gmsh, 0, L)}
    _mesh(ws, build, pick, [("root", "fixed"), ("tip", "load")])
    return ws


def test_cantilever_first_bending_frequency_is_euler_bernoulli(cantilever):
    f_eb = 1.875104 ** 2 / (2 * math.pi * L ** 2) * math.sqrt(E_STEEL * B ** 4 / 12
                                                               / (RHO_STEEL * B * B))
    dat = _solve(cantilever, "modal", f"""
*MATERIAL, NAME=STEEL
*ELASTIC
{E_STEEL}, {NU_STEEL}
*DENSITY
{RHO_STEEL}
*SOLID SECTION, ELSET=solid, MATERIAL=STEEL
*BOUNDARY
root, 1, 3, 0.0
*STEP
*FREQUENCY
2
*END STEP
""")
    block = dat.split("E I G E N V A L U E   O U T P U T")[1]
    hz = [float(p[3]) for p in (ln.split() for ln in block.splitlines())
          if len(p) == 5 and p[0].isdigit()]
    print(f"cantilever f1 {hz[0]:.3f} Hz, f2 {hz[1]:.3f} Hz vs Euler-Bernoulli {f_eb:.3f} Hz")
    assert abs(hz[0] / f_eb - 1) < 0.01, f"f1 {hz[0]:.2f} Hz vs Euler-Bernoulli {f_eb:.2f} Hz"
    assert abs(hz[1] / f_eb - 1) < 0.01            # the other bending plane, same section


def test_cantilever_tip_deflection_under_an_end_load_on_the_named_face(cantilever):
    P = 1000.0
    loads = _consistent_loads(cantilever, "tip", np.array([0.0, 0.0, P / (B * B)]))
    assert abs(sum(f[2] for f in loads.values()) / P - 1) < 1e-9   # the face holds its area
    cload = "\n".join(f"{n}, 3, {f[2]:.12g}" for n, f in sorted(loads.items()))
    dat = _solve(cantilever, "tip", f"""
*MATERIAL, NAME=STEEL
*ELASTIC
{E_STEEL}, {NU_STEEL}
*SOLID SECTION, ELSET=solid, MATERIAL=STEEL
*BOUNDARY
root, 1, 3, 0.0
*STEP
*STATIC
*CLOAD
{cload}
*NODE PRINT, NSET=tip
U
*END STEP
""")
    uz = [float(p[3]) for p in (ln.split() for ln in dat.splitlines())
          if len(p) == 4 and p[0].isdigit()]
    tip = float(np.mean(uz))
    inertia = B ** 4 / 12
    bending = P * L ** 3 / (3 * E_STEEL * inertia)
    shear = P * L / (5 / 6 * E_STEEL / (2 * (1 + NU_STEEL)) * B * B)
    print(f"cantilever tip {tip * 1e3:.4f} mm vs beam theory {(bending + shear) * 1e3:.4f} mm")
    assert abs(tip / (bending + shear) - 1) < 0.01, (
        f"tip {tip * 1e3:.4f} mm vs beam theory {(bending + shear) * 1e3:.4f} mm "
        f"(Euler-Bernoulli {bending * 1e3:.4f} mm)")


# ------------------------------------------------------------------ plate with a hole

W, D, T, HALF_L = 0.1, 0.02, 0.008, 0.075


@pytest.fixture(scope="module")
def plate(tmp_path_factory):
    ws = tmp_path_factory.mktemp("plate")

    def build(gmsh):
        occ = gmsh.model.occ
        box = occ.addBox(0, 0, 0, HALF_L, W / 2, T)
        hole = occ.addCylinder(0, 0, -T, 0, 0, 3 * T, D / 2)
        occ.cut([(3, box)], [(3, hole)])

    def pick(gmsh):
        return {"sym_x": _faces_at(gmsh, 0, 0.0), "sym_y": _faces_at(gmsh, 1, 0.0),
                "base": _faces_at(gmsh, 2, 0.0), "pull": _faces_at(gmsh, 0, HALF_L)}
    _mesh(ws, build, pick, [("sym_x", "fixed"), ("sym_y", "fixed"), ("base", "fixed"),
                      ("pull", "load")])
    return ws


def test_plate_with_a_hole_kt_is_heywood(plate):
    sigma = 1.0e6
    _solve(plate, "kt", f"""
*MATERIAL, NAME=STEEL
*ELASTIC
{E_STEEL}, 0.0
*SOLID SECTION, ELSET=solid, MATERIAL=STEEL
*BOUNDARY
sym_x, 1, 1, 0.0
sym_y, 2, 2, 0.0
base, 3, 3, 0.0
*STEP
*STATIC
*DSLOAD
pull, P, {-sigma}
*NODE FILE
S
*END STEP
""")
    frd = (plate / "kt.frd").read_text().split("STRESS")[1]
    sxx = {}
    for line in frd.splitlines():
        if line.startswith(" -1"):
            sxx[int(line[3:13])] = float(line[13:25])
        elif line.startswith(" -3"):
            break
    nodes, *_rest, nsets = _deck_sets(plate)
    # the hole's root: on the net section (x = 0) at the hole's edge, through the thickness
    root = [n for n in nsets["sym_x"] if abs(nodes[n][1] - D / 2) < 1e-9]
    assert root
    kt = float(np.mean([sxx[n] for n in root])) / (sigma * W / (W - D))
    heywood = 2 + (1 - D / W) ** 3
    print(f"plate Kt {kt:.4f} vs Heywood {heywood:.4f} ({len(root)} root nodes)")
    assert abs(kt / heywood - 1) < 0.03, f"Kt {kt:.3f} vs Heywood {heywood:.3f}"
