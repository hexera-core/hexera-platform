# Responsibility: Prove the FEA path gives the textbook answers (FEA build step 7): six classic cases, each built as CAD, meshed by the REAL FEA driver as its rules recommend, solved in CalculiX on the deck's own named faces, and compared with the closed-form answer within the design's stated tolerance.
# Boundaries: needs CalculiX. The unit shards have none and skip; CI's "FEA accuracy (CalculiX)" lane installs Ubuntu's calculix-ccx and runs this module on every change. Locally: HEXERA_CCX=~/fea_audit/ccx/ccx.
"""THE TEXTBOOK ACCURACY SUITE.

| Case | Analysis | Closed form | Tolerance |
|---|---|---|---|
| plate with a hole, d/W = 0.2 | static | Heywood net-section Kt = 2 + (1 - d/W)^3 = 2.512 | 3% |
| cantilever, L/h = 15, tip displacement | static | Timoshenko stiffness 1 / (L^3/3EI + L/kGA) | 1% |
| thick-walled cylinder, b/a = 2, internal pressure | static | Lame hoop stress at the bore p (a^2+b^2)/(b^2-a^2) | 3% |
| round bar with a U-groove, D = 50, d = 40, r = 5 (a quarter, symmetry) | static | Kt = 2.193 (converged axisymmetric reference; the published fits' 2.05-2.06 are about 6% low) | 3% |
| cantilever, L/h = 50 | modal | Euler-Bernoulli f1 and f2 (lambda L = 1.87510, 4.69409), each pair | 1% |
| column, fixed-free, L/h = 50 | buckling | Euler P_cr = pi^2 EI / 4L^2 (a dead end load: a pressure follows the face and is not Euler's problem) | 1% |

Poisson's ratio is 0 in the plate, the cantilever and the column cases, so a clamped or loaded face
adds no lateral constraint the closed form leaves out; the other cases use steel's 0.3.

Every case runs the REAL driver (engines/fea_driver.drive: measure, plan, budget, gmsh adapter,
ladder) with the mesher run in-process, so the deck solved is the deck a user downloads. The
supports and loads name the deck's own Face_k sets, picked by where their nodes lie. Each result is
appended to FEA_ACCURACY_REPORT (JSON lines) when that is set: CI prints it as the table.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

CCX = os.environ.get("HEXERA_CCX") or shutil.which("ccx") or ""
#: Set by CI's accuracy lane: there, a missing CalculiX is a failure, never a quiet skip.
REQUIRE_CCX = os.environ.get("FEA_ACCURACY_REQUIRE_CCX", "") == "1"
pytestmark = pytest.mark.skipif(not REQUIRE_CCX and not (CCX and Path(CCX).exists()),
                                reason="CalculiX (ccx) not installed - set HEXERA_CCX to run")
REPORT = os.environ.get("FEA_ACCURACY_REPORT", "")
#: THE MEMORY A SOLVE NEEDS. The static cases are LOADED (tractions, end forces - an iterative
#: solve of prescribed displacements gave reactions 5-50x off) and use CalculiX's iterative Cholesky
#: solver (its direct solver, SPOOLES, took 5-6 GB on a 100k mesh). A modal or buckling solve needs
#: the direct solver (Lanczos).
THREADS = os.environ.get("FEA_ACCURACY_THREADS", "2")
E = 210e9


# #
# the parts (millimetres, OpenCASCADE)
# #

def _box(x0, y0, z0, dx, dy, dz):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), dx, dy, dz).Shape()


def _cyl(r, h, x=0.0, y=0.0, z=0.0):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(0, 0, 1)), r, h).Shape()


def _cut(a, b):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    u = ShapeUpgrade_UnifySameDomain(BRepAlgoAPI_Cut(a, b).Shape(), True, True, True)
    u.Build()
    return u.Shape()


def _common(a, b):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    u = ShapeUpgrade_UnifySameDomain(BRepAlgoAPI_Common(a, b).Shape(), True, True, True)
    u.Build()
    return u.Shape()


def _torus(major, minor, z):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeTorus
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    return BRepPrimAPI_MakeTorus(gp_Ax2(gp_Pnt(0, 0, z), gp_Dir(0, 0, 1)), major, minor).Shape()


PLATE = {"L": 200.0, "W": 100.0, "t": 5.0, "d": 20.0}
CANT = {"L": 300.0, "h": 20.0}
LAME = {"a": 50.0, "b": 100.0, "h": 20.0, "p": 10e6}
#: THE GROOVE'S REFERENCE. No closed form exists for a U-groove; the published fits give 2.06
#: (Pilkey/Roark fit, the design's number) and 2.05 (Neuber's formula), and both are low: a fine 2D
#: axisymmetric CalculiX model (CAX8, 0.2 / 0.1 / 0.05 / 0.025 mm at the root: 2.1947 / 2.1937 /
#: 2.1935 / 2.1934; devtools/fea_reference/groove_axisymmetric.py reproduces it) converges on 2.193 -
#: independent of the product path (2D, its own mesh). That converged value is the reference here.
GROOVE = {"D": 50.0, "r": 5.0, "L": 200.0, "kt": 2.193}
SLENDER = {"L": 500.0, "h": 10.0}


def _shape(case: str):
    if case == "plate_hole":
        return _cut(_box(0, 0, 0, PLATE["L"], PLATE["W"], PLATE["t"]),
                    _cyl(PLATE["d"] / 2, PLATE["t"] + 2, PLATE["L"] / 2, PLATE["W"] / 2, -1))
    if case == "cantilever":
        return _box(0, 0, 0, CANT["L"], CANT["h"], CANT["h"])
    if case == "lame":
        ring = _cut(_cyl(LAME["b"], LAME["h"]), _cyl(LAME["a"], LAME["h"] + 2, z=-1))
        return _common(ring, _box(0, 0, -1, LAME["b"] + 1, LAME["b"] + 1, LAME["h"] + 2))
    if case == "groove":
        # a QUARTER of the bar (symmetry on its two cut planes): the groove's refinement for the
        # whole bar does not fit Quick's 100,000 elements, a quarter's does
        bar = _cut(_cyl(GROOVE["D"] / 2, GROOVE["L"]), _torus(GROOVE["D"] / 2, GROOVE["r"], GROOVE["L"] / 2))
        return _common(bar, _box(0, 0, -1, GROOVE["D"], GROOVE["D"], GROOVE["L"] + 2))
    if case in ("modal", "buckling"):
        return _box(0, 0, 0, SLENDER["h"], SLENDER["h"], SLENDER["L"])
    raise KeyError(case)


# #
# the real driver
# #

class _Run:
    async def fence(self, _why):
        return None

    def note_authoring(self):
        pass

    def note_native_run(self, produced_usable_mesh):
        pass

    def outcome(self, produced_deliverable, failure_marker="", exhausted=False):
        return SimpleNamespace(produced_deliverable=produced_deliverable, terminal_value=failure_marker)


def _mesh(case: str, ws: Path, monkeypatch) -> dict:
    import gmsh
    from tests import fea_fixtures as fx

    from meshpipeline.engines import fea_driver as FD
    from meshpipeline.engines import runtime
    from meshpipeline.engines.gmsh import fea_adapter

    ws.mkdir(parents=True, exist_ok=True)
    src = fx.write_step(_shape(case), ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)       # staged in metres, as a run stages it
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
    finally:
        gmsh.finalize()
    monkeypatch.setattr(runtime, "get_engine", lambda _n: SimpleNamespace(
        run_cartesian_mesh=lambda w, timeout: {"rc": fea_adapter.mesh(w)}))
    ok, marker, _o = asyncio.run(FD.drive(ws, {"purpose": "structural"}, job_id=f"acc-{case}", publish=None,
                                          run=_Run()))
    assert ok, f"the driver did not mesh {case}: {marker}"
    return json.loads((ws / "quality.json").read_text())


# #
# the deck, its faces, CalculiX
# #

def _deck(ws: Path):
    text = (ws / "mesh.inp").read_text()
    nodes = {}
    for ln in text.split("*NODE\n", 1)[1].split("*", 1)[0].strip().splitlines():
        p = ln.split(",")
        nodes[int(p[0])] = np.array([float(v) for v in p[1:4]])
    nsets = {m.group(1): [int(v) for v in re.findall(r"\d+", m.group(2))]
             for m in re.finditer(r"\*NSET, NSET=(Face_\d+)\n((?:[^*][^\n]*\n)*)", text)}
    return text, nodes, nsets


def _face(nodes, nsets, test) -> str:
    """The one Face_k all of whose nodes pass `test(xyz)`."""
    hits = [n for n, ids in nsets.items() if all(test(nodes[i]) for i in ids)]
    assert len(hits) == 1, f"expected one face, found {hits}"
    return hits[0]


def _pick(nodes, ids, key):
    return min(ids, key=lambda i: key(nodes[i]))


def _solve(ws: Path, job: str, deck: str, extra: str, threads: str = "") -> tuple[str, str]:
    (ws / f"{job}.inp").write_text(deck + extra)
    n = threads or THREADS
    env = {**os.environ, "OMP_NUM_THREADS": n, "CCX_NPROC_EQUATION_SOLVER": n, "CCX_NPROC_STIFFNESS": n,
           "CCX_NPROC_RESULTS": n}
    proc = subprocess.run([CCX, job], cwd=ws, capture_output=True, text=True, timeout=7200, env=env)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0 and "*ERROR" not in out, out[-2000:]
    return (ws / f"{job}.dat").read_text(), (ws / f"{job}.frd").read_text() if (ws / f"{job}.frd").exists() else ""


def _frd_stress(frd: str) -> dict[int, np.ndarray]:
    """Nodal stresses (SXX, SYY, SZZ, SXY, SYZ, SZX) from the .frd."""
    out: dict[int, np.ndarray] = {}
    block = frd.split("STRESS", 1)[1]
    for ln in block.splitlines()[1:]:
        if ln.startswith(" -3"):
            break
        if ln.startswith(" -1"):
            nid = int(ln[3:13])
            out[nid] = np.array([float(ln[13 + 12 * k:25 + 12 * k]) for k in range(6)])
    return out


def _frd_disp(frd: str) -> dict[int, np.ndarray]:
    """Nodal displacements (UX, UY, UZ) from the .frd."""
    out: dict[int, np.ndarray] = {}
    block = frd.split(" DISP ", 1)[1]
    for ln in block.splitlines()[1:]:
        if ln.startswith(" -3"):
            break
        if ln.startswith(" -1"):
            out[int(ln[3:13])] = np.array([float(ln[13 + 12 * k:25 + 12 * k]) for k in range(3)])
    return out


def _total(dat: str, comp: int) -> float:
    """A component of the total reaction force printed by *NODE PRINT, TOTALS=ONLY."""
    tail = dat.split("total force", 1)[1]
    vals = re.findall(r"^\s*(-?[\d.]+E[+-]\d+)\s+(-?[\d.]+E[+-]\d+)\s+(-?[\d.]+E[+-]\d+)\s*$", tail, re.M)[0]
    return float(vals[comp])


def _material(name: str, nu: float, rho: float | None = None) -> str:
    return (f"*MATERIAL, NAME={name}\n*ELASTIC\n{E:.6e}, {nu}\n"
            + (f"*DENSITY\n{rho}\n" if rho else "")
            + f"*SOLID SECTION, ELSET=solid, MATERIAL={name}\n")


def _record(row: dict) -> None:
    if REPORT:
        with open(REPORT, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")


# #
# the cases
# #

def _plate(ws, q):
    deck, nodes, nsets = _deck(ws)
    fixed = _face(nodes, nsets, lambda p: abs(p[0]) < 1e-9)
    load = _face(nodes, nsets, lambda p: abs(p[0] - PLATE["L"] * 1e-3) < 1e-9)
    ids = nsets[fixed]
    a = _pick(nodes, ids, lambda p: p[1] + p[2])
    b = _pick(nodes, ids, lambda p: -p[1] + p[2])
    c = _pick(nodes, ids, lambda p: p[1] - p[2])
    gross = 1.0e6                                   # 1 MPa of tension on the end face
    _dat, frd = _solve(ws, "kt", deck, _material("STEEL", 0.0) + f"""*NSET, NSET=PA
{a}
*NSET, NSET=PB
{b}
*NSET, NSET=PC
{c}
*BOUNDARY
{fixed}, 1, 1, 0.0
PA, 2, 3, 0.0
PB, 3, 3, 0.0
PC, 2, 2, 0.0
*STEP
*STATIC, SOLVER=ITERATIVE CHOLESKY
*DSLOAD
{load}, P, {-gross:.6e}
*NODE FILE
S
*END STEP
""")
    smax = max(st[0] for st in _frd_stress(frd).values())
    net = gross * PLATE["W"] / (PLATE["W"] - PLATE["d"])
    return smax / net, 2.0 + (1 - PLATE["d"] / PLATE["W"]) ** 3, "net-section Kt"


def _cantilever(ws, q):
    deck, nodes, nsets = _deck(ws)
    root = _face(nodes, nsets, lambda p: abs(p[0]) < 1e-9)
    tip = _face(nodes, nsets, lambda p: abs(p[0] - CANT["L"] * 1e-3) < 1e-9)
    force = 1000.0                                  # 1 kN down the end face, shared over its nodes
    ids = nsets[tip]
    cloads = "\n".join(f"{i}, 3, {force / len(ids):.12e}" for i in ids)
    _dat, frd = _solve(ws, "cb", deck, _material("STEEL", 0.0) + f"""*BOUNDARY
{root}, 1, 3, 0.0
*STEP
*STATIC, SOLVER=ITERATIVE CHOLESKY
*CLOAD
{cloads}
*NODE FILE
U
*END STEP
""")
    u = _frd_disp(frd)
    delta = float(np.mean([u[i][2] for i in ids]))
    L, h = CANT["L"] * 1e-3, CANT["h"] * 1e-3
    inertia, area, G, kappa = h ** 4 / 12, h * h, E / 2.0, 5.0 / 6.0
    k_exact = 1.0 / (L ** 3 / (3 * E * inertia) + L / (kappa * G * area))
    return force / delta, k_exact, "tip stiffness (N/m)"


def _lame(ws, q):
    deck, nodes, nsets = _deck(ws)
    a, b, h, p = LAME["a"] * 1e-3, LAME["b"] * 1e-3, LAME["h"] * 1e-3, LAME["p"]
    sx = _face(nodes, nsets, lambda x: abs(x[0]) < 1e-9)
    sy = _face(nodes, nsets, lambda x: abs(x[1]) < 1e-9)
    bot = _face(nodes, nsets, lambda x: abs(x[2]) < 1e-9)
    top = _face(nodes, nsets, lambda x: abs(x[2] - h) < 1e-9)
    bore = _face(nodes, nsets, lambda x: abs(math.hypot(x[0], x[1]) - a) < 1e-6)
    dat, frd = _solve(ws, "lame", deck, _material("STEEL", 0.3) + f"""*BOUNDARY
{sx}, 1, 1, 0.0
{sy}, 2, 2, 0.0
{bot}, 3, 3, 0.0
{top}, 3, 3, 0.0
*STEP
*STATIC, SOLVER=ITERATIVE CHOLESKY
*DSLOAD
{bore}, P, {p:.6e}
*NODE FILE
S
*END STEP
""")
    st = _frd_stress(frd)
    hoop = []
    for i in nsets[bore]:
        x, y, _z = nodes[i]
        r = math.hypot(x, y)
        c, s = x / r, y / r
        sxx, syy, _szz, sxy, _syz, _szx = st[i]
        hoop.append(sxx * s * s + syy * c * c - 2 * sxy * s * c)
    return float(np.mean(hoop)) / p, (a * a + b * b) / (b * b - a * a), "bore hoop stress / p"


def _groove(ws, q):
    deck, nodes, nsets = _deck(ws)
    L = GROOVE["L"] * 1e-3
    bot = _face(nodes, nsets, lambda x: abs(x[2]) < 1e-9)
    top = _face(nodes, nsets, lambda x: abs(x[2] - L) < 1e-9)
    sx = _face(nodes, nsets, lambda x: abs(x[0]) < 1e-9)
    sy = _face(nodes, nsets, lambda x: abs(x[1]) < 1e-9)
    gross = 1.0e6                                   # 1 MPa of tension on the end face
    _dat, frd = _solve(ws, "groove", deck, _material("STEEL", 0.3) + f"""*BOUNDARY
{bot}, 3, 3, 0.0
{sx}, 1, 1, 0.0
{sy}, 2, 2, 0.0
*STEP
*STATIC, SOLVER=ITERATIVE CHOLESKY
*DSLOAD
{top}, P, {-gross:.6e}
*NODE FILE
S
*END STEP
""")
    d = GROOVE["D"] - 2 * GROOVE["r"]
    smax = max(st[2] for st in _frd_stress(frd).values())
    return smax / (gross * (GROOVE["D"] / d) ** 2), GROOVE["kt"], "net-section Kt (converged axisymmetric reference)"


def _slender_props():
    L, h = SLENDER["L"] * 1e-3, SLENDER["h"] * 1e-3
    return L, h, h ** 4 / 12, h * h


def _modal(ws, q):
    deck, nodes, nsets = _deck(ws)
    root = _face(nodes, nsets, lambda x: abs(x[2]) < 1e-9)
    rho = 7850.0
    dat, _frd = _solve(ws, "modal", deck, _material("STEEL", 0.3, rho) + f"""*BOUNDARY
{root}, 1, 3, 0.0
*STEP
*FREQUENCY
6
*END STEP
""", threads="1")
    # ONE THREAD, as the column: on 4 threads CalculiX 2.21's eigensolve once returned a 0 Hz mode
    block = dat.split("E I G E N V A L U E   O U T P U T", 1)[1]
    freqs = sorted(float(m.group(1)) for m in re.finditer(
        r"^\s*\d+\s+[-\d.E+]+\s+[-\d.E+]+\s+([-\d.E+]+)\s+[-\d.E+]+\s*$", block, re.M))
    L, _h, inertia, area = _slender_props()
    base = math.sqrt(E * inertia / (rho * area)) / (2 * math.pi * L * L)
    f1, f2 = 1.87510 ** 2 * base, 4.69409 ** 2 * base
    # the square column bends both ways at the same frequency: modes 1-2 are the first bending
    # pair, 3-4 the second (torsion and stretching come far above). Each pair is held to its own
    # answer, and the worse of the two is reported.
    assert len(freqs) >= 4, freqs
    pairs = [(sum(freqs[0:2]) / 2, f1, "first bending frequency (Hz)"),
             (sum(freqs[2:4]) / 2, f2, "second bending frequency (Hz)")]
    for got, want, _what in pairs:
        assert abs(freqs[1] / freqs[0] - 1) < 0.01 and abs(freqs[3] / freqs[2] - 1) < 0.01, freqs
    return max(pairs, key=lambda t: abs(t[0] / t[1] - 1))


def _buckling(ws, q):
    deck, nodes, nsets = _deck(ws)
    L, _h, inertia, area = _slender_props()
    root = _face(nodes, nsets, lambda x: abs(x[2]) < 1e-9)
    tip = _face(nodes, nsets, lambda x: abs(x[2] - L) < 1e-9)
    # A DEAD end load: 1 N in -z shared over the end face's nodes. A pressure would follow the end
    # face as it turns (a follower load: Beck's column, which has no static Euler load at all), and
    # the first two "equal" modes of the square column came out 13% apart that way.
    ids = nsets[tip]
    share = 1.0 / len(ids)
    cloads = "\n".join(f"{i}, 3, {-share:.12e}" for i in ids)
    dat, _frd = _solve(ws, "buckle", deck, _material("STEEL", 0.0) + f"""*BOUNDARY
{root}, 1, 3, 0.0
*STEP
*BUCKLE
4, 1.0E-7
*CLOAD
{cloads}
*END STEP
""", threads="1")
    # ONE THREAD: CalculiX 2.21's buckling run on 4 threads returned factors of about 1.0 for this
    # column (garbage; 2 threads and 1 gave 1,726.7 N) - so the column is solved on one
    block = dat.split("B U C K L I N G   F A C T O R   O U T P U T", 1)[1]
    factors = [float(m.group(1)) for m in re.finditer(r"^\s*\d+\s+([-\d.E+]+)\s*$", block, re.M)]
    p_cr = min(f for f in factors if f > 0) * 1.0
    return p_cr, math.pi ** 2 * E * inertia / (4 * L * L), "Euler critical load (N)"


CASES = {
    "plate_hole": ("static", _plate, 0.03),
    "cantilever": ("static", _cantilever, 0.01),
    "lame": ("static", _lame, 0.03),
    "groove": ("static", _groove, 0.03),
    "modal": ("modal", _modal, 0.01),
    "buckling": ("buckling", _buckling, 0.01),
}


@pytest.mark.parametrize("case", list(CASES))
def test_the_fea_path_gives_the_textbook_answer(case, tmp_path, monkeypatch):
    assert CCX and Path(CCX).exists(), f"CalculiX is required here and was not found ({CCX or 'no ccx'})"
    analysis, run, tol = CASES[case]
    q = _mesh(case, tmp_path / case, monkeypatch)
    got, want, what = run(tmp_path / case, q)
    err = got / want - 1.0
    row = {"case": case, "analysis": analysis, "elements": q["cells"], "what": what,
           "got": float(got), "want": float(want), "error_pct": round(100 * err, 2), "tolerance_pct": round(100 * tol, 1),
           "pass": bool(abs(err) <= tol)}
    _record(row)
    print(json.dumps(row))
    assert abs(err) <= tol, f"{case}: {what} {got:.5g} vs {want:.5g} ({100 * err:+.2f}%, tolerance {100 * tol:.0f}%)"
