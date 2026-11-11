# Responsibility: Prove the SURFACE-FILE route against textbook answers in CalculiX (SURF-3): a sheet given as a
# surface with a thickness in words goes through cad/surface_model (sewn, the shell record), the shell mesher and
# the CalculiX writer, is loaded and solved - a cantilever plate (PL^3/3EI), a plate with a hole in tension
# (Heywood's net-section Kt) and a pressurised open cylinder (pr/t). The same three cases the solid route's
# mid-surfaces are held to (test_shell_pipeline.py), fed as surfaces.
# Boundaries: needs CalculiX (HEXERA_CCX, or ccx on the path; CI's accuracy lane runs it). Millimetres, N, MPa.
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")
pytest.importorskip("gmsh")

from tests import surface_fixtures as sx  # noqa: E402

CCX = os.environ.get("HEXERA_CCX") or shutil.which("ccx") or ""
REQUIRE_CCX = os.environ.get("FEA_ACCURACY_REQUIRE_CCX", "") == "1"
REPORT = os.environ.get("FEA_ACCURACY_REPORT", "")
pytestmark = pytest.mark.skipif(not REQUIRE_CCX and not (CCX and Path(CCX).exists()),
                                reason="CalculiX (ccx) not installed - set HEXERA_CCX to run")
E, NU, RHO = 210000.0, 0.3, 7.85e-9


def _record(case: str, what: str, got: float, want: float, tol: float, q: dict) -> None:
    """One row of the FEA accuracy lane's table (the keys test_shell_pipeline's rows carry)."""
    if REPORT:
        err = got / want - 1.0
        row = {"suite": "surface_route", "case": case, "analysis": "static (S4)", "elements": int(q["elements"]),
               "what": what, "got": float(got), "want": float(want), "error_pct": round(100 * err, 2),
               "tolerance_pct": round(100 * tol, 1), "pass": bool(abs(err) <= tol and q["within_nastran"]),
               "within_nastran": q["within_nastran"]}
        with open(REPORT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")


def _shell(shape, ws: Path, name: str, thickness_mm: float):
    """The surface through its route: (its shell mesh, the CalculiX mesh deck text, the mesh's quality)."""
    from meshpipeline.cad import midsurface as ms
    from meshpipeline.cad import surface_model as sm
    from meshpipeline.engines import shell_writers as sw
    from meshpipeline.engines.gmsh import shell_adapter as sa

    reading, sewn = sm.read(shape, mm_per_unit=1.0)
    assert reading["kind"] == "surface", reading["why"]
    rec, surface = sm.record(reading, sewn, mm_per_unit=1.0,
                             walls_mm={s["part"]: thickness_mm for s in reading["sheets"]})
    out = ws / name
    sm.write(rec, surface, out)
    mesh, applied = sa.mesh_shell(out / ms.SURFACE_FILE, rec)
    assert set(np.round(mesh.wall, 9)) == {thickness_mm}
    prep = sw.prepare(mesh, units="mm")
    mats = {i: ["*ELASTIC", f"{E}, {NU}", "*DENSITY", f"{RHO}"] for i in range(1, len(prep.parts) + 1)}
    sw.write_inp(ws / f"{name}_mesh.inp", prep, units="mm", materials=mats)
    return mesh, (ws / f"{name}_mesh.inp").read_text(), applied["quality"]


def _solve(ws: Path, job: str, deck: str) -> tuple[str, str]:
    (ws / f"{job}.inp").write_text(deck)
    env = {**os.environ, "OMP_NUM_THREADS": "1", "CCX_NPROC_EQUATION_SOLVER": "1"}
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


def test_a_cantilever_given_as_a_surface_bends_as_beam_theory_says(tmp_path):
    L, B, T, P = 200.0, 20.0, 2.0, 10.0
    mesh, deck, q = _shell(sx.plate(L, B), tmp_path, "cant", T)
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
    _record("surface cantilever plate", "tip deflection (mm)", float(-uz.mean()), beam, 0.03, q)
    assert q["within_nastran"]
    # a plate strip 10 walls wide sits between the beam (E) and the wide plate (E / (1 - nu^2), 9% stiffer)
    assert -0.03 <= off <= 0.01, off


def test_a_plate_with_a_hole_given_as_a_surface_has_heywoods_kt(tmp_path):
    W, Lp, T, R, S = 100.0, 200.0, 3.0, 10.0, 10.0
    mesh, deck, q = _shell(sx.plate_with_hole(Lp, W, R), tmp_path, "kt", T)
    left, right = _nodes(mesh, lambda p: abs(p[0]) < 1e-6), _nodes(mesh, lambda p: abs(p[0] - Lp) < 1e-6)
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
    _record("surface plate with a hole", "Kt (Heywood)", float(kt), heywood, 0.06, q)
    assert q["within_nastran"]
    assert abs(kt / heywood - 1.0) <= 0.06, (kt, heywood)


def test_a_pressurised_open_cylinder_given_as_a_surface_carries_pr_over_t(tmp_path):
    r, T, H, p = 49.0, 2.0, 300.0, 1.0
    mesh, deck, q = _shell(sx.open_tube(r, H), tmp_path, "cyl", T)
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
    _record("surface pressurised cylinder", "radial growth (mm)", float(radial.mean()), theory, 0.02, q)
    assert q["within_nastran"]
    assert abs(radial.mean() / theory - 1.0) <= 0.02, (radial.mean(), theory)
