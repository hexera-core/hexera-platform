# Responsibility: Prove the WHOLE shell pipeline against textbook answers in CalculiX: a solid CAD part goes through
# the mid-surface extraction (cad/midsurface.py), the shell mesh (engines/gmsh/shell_adapter.py) and the CalculiX
# writer (engines/shell_writers.py), then is loaded and solved - a cantilever plate (PL^3/3EI), a plate with a hole
# in tension (Heywood's net-section Kt), a pressurised open cylinder (pr/t), and a stiffened panel's free-free modes
# against a fine structured solid (C3D20R) model of the same part.
# Boundaries: needs CalculiX (HEXERA_CCX, or ccx on the path; CI's accuracy lane runs it). Millimetres, N, MPa.
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from tests import shell_fixtures as sf
from tests.fea_fixtures import _box

CCX = os.environ.get("HEXERA_CCX") or shutil.which("ccx") or ""
REQUIRE_CCX = os.environ.get("FEA_ACCURACY_REQUIRE_CCX", "") == "1"
REPORT = os.environ.get("FEA_ACCURACY_REPORT", "")
pytestmark = pytest.mark.skipif(not REQUIRE_CCX and not (CCX and Path(CCX).exists()),
                                reason="CalculiX (ccx) not installed - set HEXERA_CCX to run")
E, NU, RHO = 210000.0, 0.3, 7.85e-9


def _record(row: dict) -> None:
    if REPORT:
        with open(REPORT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"suite": "shell_pipeline", **row}) + "\n")


def _shell(shape, ws: Path, name: str):
    """The part through the pipeline: (its shell mesh, the CalculiX mesh deck text, the mesh's quality)."""
    from OCP.BRepTools import BRepTools

    from meshpipeline.cad import midsurface as ms
    from meshpipeline.engines import shell_writers as sw
    from meshpipeline.engines.gmsh import shell_adapter as sa

    rec, surface = ms.extract(shape, mm_per_unit=1.0)
    assert rec["checks"]["connected"], rec["sewn"]
    brep = ws / f"{name}.brep"
    BRepTools.Write_s(surface, str(brep))
    mesh, applied = sa.mesh_shell(brep, rec)
    prep = sw.prepare(mesh, units="mm")
    mats = {i: ["*ELASTIC", f"{E}, {NU}", "*DENSITY", f"{RHO}"] for i in range(1, len(prep.parts) + 1)}
    sw.write_inp(ws / f"{name}_mesh.inp", prep, units="mm", materials=mats)
    return mesh, (ws / f"{name}_mesh.inp").read_text(), applied["quality"]


def _solve(ws: Path, job: str, deck: str) -> tuple[str, str]:
    (ws / f"{job}.inp").write_text(deck)
    env = {**os.environ, "OMP_NUM_THREADS": "2", "CCX_NPROC_EQUATION_SOLVER": "2"}
    proc = subprocess.run([CCX, job], cwd=ws, capture_output=True, text=True, timeout=3600, env=env)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0 and "*ERROR" not in out, out[-2000:]
    frd = ws / f"{job}.frd"
    return (ws / f"{job}.dat").read_text(), frd.read_text() if frd.exists() else ""


def _frd_block(frd: str, name: str, k: int) -> dict[int, np.ndarray]:
    out: dict[int, np.ndarray] = {}
    block = frd.split(name, 1)[1]
    for ln in block.splitlines()[1:]:
        if ln.startswith(" -3"):
            break
        if ln.startswith(" -1"):
            out[int(ln[3:13])] = np.array([float(ln[13 + 12 * i:25 + 12 * i]) for i in range(k)])
    return out


def _nset(name: str, ids) -> str:
    ids = [int(i) for i in ids]
    assert ids, name
    return f"*NSET, NSET={name}\n" + "\n".join(", ".join(map(str, ids[i:i + 12])) for i in range(0, len(ids), 12)) + "\n"


def _nodes(mesh, test) -> list[int]:
    return [i + 1 for i, p in enumerate(mesh.xyz) if test(p)]


# ------------------------------------------------------------------------------------- the cantilever ----
def test_a_cantilever_plate_bends_as_beam_theory_says(tmp_path):
    L, B, T, P = 200.0, 20.0, 2.0, 10.0
    mesh, deck, q = _shell(_box(0, 0, 0, L, B, T), tmp_path, "cant")
    root, tip = _nodes(mesh, lambda p: abs(p[0]) < 1e-6), _nodes(mesh, lambda p: abs(p[0] - L) < 1e-6)
    dat, _frd = _solve(tmp_path, "cant", deck + _nset("ROOT", root) + _nset("TIP", tip) + """*BOUNDARY
ROOT, 1, 6
*STEP
*STATIC
*CLOAD
""" + "".join(f"{i}, 3, {-P / len(tip):.9e}\n" for i in tip) + """*NODE PRINT, NSET=TIP
U
*END STEP
""")
    rows = [r.split() for r in dat.splitlines()]
    uz = np.array([float(r[3]) for r in rows if len(r) == 4 and r[0].isdigit()])
    beam = P * L ** 3 / (3 * E * B * T ** 3 / 12)
    off = float(-uz.mean() / beam - 1.0)
    _record({"case": "cantilever plate", "measured_mm": float(-uz.mean()), "theory_mm": beam, "off": off,
             "elements": q["elements"], "within_nastran": q["within_nastran"]})
    assert q["within_nastran"]
    # a plate strip 10 walls wide sits between the beam (E) and the wide plate (E / (1 - nu^2), 9% stiffer)
    assert -0.03 <= off <= 0.01, off


# -------------------------------------------------------------------------------- the plate with a hole ----
def test_a_plate_with_a_hole_in_tension_has_heywoods_kt(tmp_path):
    W, Lp, T, R, S = 100.0, 200.0, 3.0, 10.0, 10.0
    mesh, deck, q = _shell(sf.plate_with_hole(T, R), tmp_path, "kt")
    left, right = _nodes(mesh, lambda p: abs(p[0]) < 1e-6), _nodes(mesh, lambda p: abs(p[0] - Lp) < 1e-6)
    # the end load shared by each node's length of edge
    ys = np.array([mesh.xyz[i - 1][1] for i in right])
    order = np.argsort(ys)
    y = ys[order]
    share = np.zeros(len(y))
    share[:-1] += 0.5 * np.diff(y)
    share[1:] += 0.5 * np.diff(y)
    force = S * W * T
    loads = "".join(f"{right[o]}, 1, {force * share[k] / share.sum():.9e}\n" for k, o in enumerate(order))
    corner = min(left, key=lambda i: mesh.xyz[i - 1][1])
    _dat, frd = _solve(tmp_path, "kt", deck + _nset("LEFT", left) + f"""*BOUNDARY
LEFT, 1, 1
LEFT, 3, 3
LEFT, 4, 6
{corner}, 2, 2
*STEP
*STATIC
*CLOAD
{loads}*NODE FILE, OUTPUT=2D
S
*END STEP
""")
    st = _frd_block(frd, "STRESS", 6)
    smax = max(v[0] for v in st.values())
    kt = smax / (S * W / (W - 2 * R))
    heywood = 2.0 + (1.0 - 2 * R / W) ** 3
    off = float(kt / heywood - 1.0)
    _record({"case": "plate with a hole", "measured_kt": float(kt), "theory_kt": heywood, "off": off,
             "elements": q["elements"], "within_nastran": q["within_nastran"]})
    assert q["within_nastran"]
    assert abs(off) <= 0.06, (kt, heywood)


# ------------------------------------------------------------------------------ the pressurised cylinder ----
def test_a_pressurised_open_cylinder_carries_pr_over_t(tmp_path):
    r_out, T, H, p = 50.0, 2.0, 300.0, 1.0
    r = r_out - T / 2
    mesh, deck, q = _shell(sf.tube(r_out, T, H), tmp_path, "cyl")
    # the pressure pushes outward: CalculiX's P acts along the element normal, so its sign follows the normals
    from meshpipeline.engines.shell_mesh import _normal

    out = np.mean([np.sign(np.dot(_normal(mesh.xyz, c), np.r_[mesh.xyz[c].mean(0)[:2], 0.0])) for c in mesh.elements()])
    sign = 1.0 if out > 0 else -1.0
    base = _nodes(mesh, lambda x: abs(x[2]) < 1e-6)

    def near(a):
        return min(base, key=lambda i: float(np.linalg.norm(mesh.xyz[i - 1][:2] - a)))

    a, b, c = near(np.array([r, 0.0])), near(np.array([0.0, r])), near(np.array([-r, 0.0]))
    mid = _nodes(mesh, lambda x: 100.0 <= x[2] <= 200.0)
    _dat, frd = _solve(tmp_path, "cyl", deck + _nset("BASE", base) + _nset("MID", mid) + f"""*BOUNDARY
BASE, 3, 3
{a}, 2, 2
{b}, 1, 1
{c}, 2, 2
*STEP
*STATIC
*DLOAD
EALL, P, {sign * p:.6e}
*NODE FILE, OUTPUT=2D
U
*END STEP
""")
    u = _frd_block(frd, " DISP ", 3)
    xy = np.array([mesh.xyz[i - 1][:2] for i in mid])
    radial = np.array([np.dot(u[i][:2], xy[k] / np.linalg.norm(xy[k])) for k, i in enumerate(mid)])
    theory = r * (p * r / T) / E
    off = float(radial.mean() / theory - 1.0)
    _record({"case": "pressurised cylinder", "measured_mm": float(radial.mean()), "theory_mm": theory, "off": off,
             "elements": q["elements"], "within_nastran": q["within_nastran"]})
    assert q["within_nastran"]
    assert abs(off) <= 0.02, off


# ---------------------------------------------------------------------------- the stiffened panel's modes ----
def _frequencies(dat: str) -> list[float]:
    block = dat.split("E I G E N V A L U E   O U T P U T", 1)[1]
    out = []
    for ln in block.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$", ln)
        if m:
            out.append(float(m.group(4)))
        elif out and not ln.strip():
            break
    return out


def _solid_panel(ws: Path, tp: float, ts: float, h: float, n: int) -> list[float]:
    """The panel as a structured C3D20R solid (2 elements through the plate, 2 across each stiffener), free-free."""
    from tests.unit.engines.test_shell_textbook import Deck

    d = Deck()
    xs_st = [400.0 * (i + 1) / (n + 1) - ts / 2 for i in range(n)]
    fixed = [v for x0 in xs_st for v in (x0, x0 + ts / 2, x0 + ts)]
    even = [x for x in np.linspace(0, 400, 41).tolist() if all(abs(x - v) > 2.0 for v in fixed)]
    xs = sorted({round(x, 6) for x in even + fixed + [0.0, 400.0]})
    ys = np.linspace(0, 300, 31).tolist()
    d.box(xs, ys, [0.0, tp / 2, tp])
    for x0 in xs_st:
        d.box([x0, x0 + ts / 2, x0 + ts], ys, np.linspace(tp, tp + h, 6).tolist())
    lines = ["*NODE, NSET=NALL"] + [f"{i + 1}, {x}, {y}, {z}" for i, (x, y, z) in enumerate(d.xyz)]
    lines.append("*ELEMENT, TYPE=C3D20R, ELSET=SO")
    for k, conn in enumerate(d.solid, start=1):
        vals = [k, *conn]
        lines += [", ".join(map(str, vals[i:i + 15])) + ("," if i + 15 < len(vals) else "")
                  for i in range(0, len(vals), 15)]
    lines += ["*MATERIAL, NAME=STEEL", "*ELASTIC", f"{E}, {NU}", "*DENSITY", f"{RHO}",
              "*SOLID SECTION, ELSET=SO, MATERIAL=STEEL", "*STEP", "*FREQUENCY", "12", "*END STEP"]
    dat, _ = _solve(ws, "panel_solid", "\n".join(lines) + "\n")
    return _frequencies(dat)


def test_a_stiffened_panel_rings_at_the_solid_parts_frequencies(tmp_path):
    tp, ts, h, n = 6.0, 8.0, 50.0, 3
    mesh, deck, q = _shell(sf.stiffened_panel(tp, ts, h, n), tmp_path, "panel")
    dat, _ = _solve(tmp_path, "panel", deck + "*STEP\n*FREQUENCY\n12\n*END STEP\n")
    shell = _frequencies(dat)[6:12]
    solid = _solid_panel(tmp_path, tp, ts, h, n)[6:12]
    off = [float(a / b - 1.0) for a, b in zip(shell, solid, strict=True)]
    _record({"case": "stiffened panel free-free modes 7-12", "shell_hz": shell, "solid_hz": solid, "off": off,
             "elements": q["elements"], "within_nastran": q["within_nastran"]})
    assert q["within_nastran"]
    assert max(abs(x) for x in off[:4]) <= 0.05, off
