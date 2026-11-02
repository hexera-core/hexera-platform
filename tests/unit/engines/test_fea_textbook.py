# Responsibility: Prove the FEA path gives the textbook answers (FEA build step 7): nine classic cases (two of them assemblies), each built as CAD, meshed by the REAL FEA driver as its rules recommend, solved in CalculiX on the deck's own named faces, and compared with the closed-form answer within the design's stated tolerance.
# Boundaries: needs CalculiX. The unit shards have none and skip; CI's "FEA accuracy (CalculiX)" lane installs Ubuntu's calculix-ccx and runs this module on every change. Locally: HEXERA_CCX=~/fea_audit/ccx/ccx.
"""THE TEXTBOOK ACCURACY SUITE.

| Case | Analysis | Closed form | Tolerance |
|---|---|---|---|
| plate with a hole, d/W = 0.2 | static | Heywood net-section Kt = 2 + (1 - d/W)^3 = 2.512 | 3% |
| the same plate with ribs, meshed QUICKER (PLAN-fea-22: their capped corners coarsened) | static | the same Kt at the hole | 3% |
| the same plate, its ribs' tops rounded, meshed QUICKER (rounds and capped corners coarsened) | static | the same Kt at the hole | 3% |
| stepped round bar, D = 60, d = 40, shoulder fillet r = 5 (a quarter), full rules and QUICKER (its convex rounds coarsened) | static | Kt = 1.878 (converged axisymmetric reference; the Pilkey fit gives 1.750, 7% low) | 3% |
| the U-grooved bar with rounded ends, meshed QUICKER (its end rounds coarsened) | static | the groove's Kt = 2.193 | 3% |
| cantilever, L/h = 15, tip displacement | static | Timoshenko stiffness 1 / (L^3/3EI + L/kGA) | 1% |
| thick-walled cylinder, b/a = 2, internal pressure | static | Lame hoop stress at the bore p (a^2+b^2)/(b^2-a^2) | 3% |
| round bar with a U-groove, D = 50, d = 40, r = 5 (a quarter, symmetry) | static | Kt = 2.193 (converged axisymmetric reference; the published fits' 2.05-2.06 are about 6% low) | 3% |
| cantilever, L/h = 50 | modal | Euler-Bernoulli f1 and f2 (lambda L = 1.87510, 4.69409), each pair | 1% |
| column, fixed-free, L/h = 50 | buckling | Euler P_cr = pi^2 EI / 4L^2 (a dead end load: a pressure follows the face and is not Euler's problem) | 1% |
| ASSEMBLY: two 100 mm blocks, bonded, under tension | static | one 200 mm bar: end displacement p L / E, and sigma_xx = p on both sides of the joint | 1% |
| ASSEMBLY: two 100 mm blocks in contact, under compression | static | sigma_xx = -p in both parts (the load crosses the contact pair) | 1% |
| ASSEMBLY: three bonded blocks, two of them also tied along a line, under tension | static | the reaction balances the pull (0.1%); sigma_xx = p at the pulled end | 1% |
| ASSEMBLY: three 100 mm blocks, A-B bonded, A-C set Off (PLAN-fea-19), B pulled | static | A and B as one 200 mm bar (p L / E, sigma_xx = p in B); C unstressed | 1% |

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
#: PLAN-fea-22'S EVIDENCE: four 2 mm ribs on the plate near its held end (16 capped sharp corners at their roots),
#: and the quicker mesh's cap as a share of the full-rules estimate - low enough that the corners are coarsened,
#: high enough that nothing else is (the plan records it: the case checks it)
RIBS = {"n": 4, "x0": 8.0, "pitch": 9.0, "w": 4.0, "h": 2.0, "share": 0.6}
#: the ribs' tops rounded (PLAN-fea-7 rounds, coarsened first by the quicker mesh)
RIB_ROUND = 1.0
#: THE STEPPED SHAFT (a quarter, symmetry): d = 40 for half its length, D = 60 for the rest, the shoulder filleted
#: r = 5, the shoulder's outer edge and both ends rounded 2 mm (rounds kept for their shape). Kt is the largest axial
#: stress over the small section's mean axial stress. No closed form exists; the Pilkey fit for a shoulder fillet in
#: tension (chart 3.4: h/r = 2, 2h/D = 1/3) gives 1.750, and a fine 2D axisymmetric CalculiX model of the same
#: shoulder (devtools/fea_reference/shoulder_axisymmetric.py: 0.2 / 0.1 / 0.05 / 0.025 mm at the fillet: 1.8790 / 1.8785 /
#: 1.8780 / 1.8780) converges on 1.878 - the fit is 7% low, as the groove's are, so the converged value is the reference.
SHAFT = {"D": 60.0, "d": 40.0, "r": 5.0, "L": 160.0, "round": 2.0, "kt": 1.878}
#: THE QUICKER MESH'S CAP in the textbook cases, as a share of the part's full-rules estimate: the lowest share (in
#: steps of 0.05, searched on the lab, 28 Oct) at which the quicker mesh still coarsens only rounds, relief and capped
#: corners - so each case coarsens its low-stress zones as far as PLAN-fea-22 ever recommends; the case checks that
#: the plan coarsened something, and nothing else
QUICKER_SHARE = {"plate_hole_quicker": RIBS["share"], "plate_hole_rounds_quicker": 0.7, "shaft_fillet_quicker": 0.8,
                 "groove_quicker": 0.85}
CANT = {"L": 300.0, "h": 20.0}
LAME = {"a": 50.0, "b": 100.0, "h": 20.0, "p": 10e6}
#: THE GROOVE'S REFERENCE. No closed form exists for a U-groove; the published fits give 2.06
#: (Pilkey/Roark fit, the design's number) and 2.05 (Neuber's formula), and both are low: a fine 2D
#: axisymmetric CalculiX model (CAX8, 0.2 / 0.1 / 0.05 / 0.025 mm at the root: 2.1947 / 2.1937 /
#: 2.1935 / 2.1934; devtools/fea_reference/groove_axisymmetric.py reproduces it) converges on 2.193 -
#: independent of the product path (2D, its own mesh). That converged value is the reference here.
GROOVE = {"D": 50.0, "r": 5.0, "L": 200.0, "kt": 2.193}
SLENDER = {"L": 500.0, "h": 10.0}
#: THE ASSEMBLY CASES (PLAN-fea-17): two cubes of this side (mm) along x, and the end pressure (Pa)
BLOCKS = {"a": 100.0, "p": 1.0e6, "gap": 0.2}
#: how each assembly case's parts are joined (the stage's one answer)
CONNECTION = {"bonded_tension": "bonded", "contact_compression": "contact", "bonded_gap_tension": "bonded",
              "bonded_corner_tie": "bonded", "pair_off_carries_nothing": "bonded"}
#: THE CORNER CASE: tests/fea_fixtures.corner_blocks of this section (mm)
CORNER = {"a": 50.0}
#: PLAN-fea-19: the single pairs set on the stage, per case (A-C set Off, A-B left bonded)
PAIRS = {"pair_off_carries_nothing": {"Part_1|Part_3": {"type": "off"}}}


def _round_edges(shape, radius, pick):
    """`shape` with the straight edges whose two ends (a, b) pass `pick` rounded to `radius`."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp, TopExp_Explorer
    from OCP.TopoDS import TopoDS

    mk = BRepFilletAPI_MakeFillet(shape)
    ex = TopExp_Explorer(shape, TopAbs_EDGE)
    while ex.More():
        e = TopoDS.Edge_s(ex.Current())
        a, b = (BRep_Tool.Pnt_s(v) for v in (TopExp.FirstVertex_s(e), TopExp.LastVertex_s(e)))
        if pick((a.X(), a.Y(), a.Z()), (b.X(), b.Y(), b.Z())):
            mk.Add(radius, e)
        ex.Next()
    mk.Build()
    return mk.Shape()


def _round_circles(shape, picks):
    """`shape` with each circular edge of radius R at height z rounded to r, for (R, z, r) in `picks`."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.GeomAbs import GeomAbs_Circle
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    mk = BRepFilletAPI_MakeFillet(shape)
    ex = TopExp_Explorer(shape, TopAbs_EDGE)
    seen = set()
    while ex.More():
        e = TopoDS.Edge_s(ex.Current())
        c = BRepAdaptor_Curve(e)
        if c.GetType() == GeomAbs_Circle:
            circ = c.Circle()
            for big_r, z, r in picks:
                key = (round(circ.Radius(), 6), round(circ.Location().Z(), 6))
                if abs(circ.Radius() - big_r) < 1e-6 and abs(circ.Location().Z() - z) < 1e-6 and key not in seen:
                    seen.add(key)
                    mk.Add(r, e)
        ex.Next()
    mk.Build()
    return mk.Shape()


def _shaft():
    """A QUARTER of the stepped shaft (SHAFT): d up to half its length, D after, the shoulder filleted r, its outer
    edge and both ends rounded."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    half = SHAFT["L"] / 2
    shaft = BRepAlgoAPI_Fuse(_cyl(SHAFT["d"] / 2, half + 1.0), _cyl(SHAFT["D"] / 2, half, z=half)).Shape()
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    u = ShapeUpgrade_UnifySameDomain(shaft, True, True, True)
    u.Build()
    shaft = _round_circles(u.Shape(), [(SHAFT["d"] / 2, half, SHAFT["r"])])
    shaft = _round_circles(shaft, [(SHAFT["D"] / 2, half, SHAFT["round"]), (SHAFT["d"] / 2, 0.0, SHAFT["round"]),
                                   (SHAFT["D"] / 2, SHAFT["L"], SHAFT["round"])])
    return _common(shaft, _box(0, 0, -1, SHAFT["D"], SHAFT["D"], SHAFT["L"] + 2))


def _shape(case: str):
    if case == "plate_hole":
        return _cut(_box(0, 0, 0, PLATE["L"], PLATE["W"], PLATE["t"]),
                    _cyl(PLATE["d"] / 2, PLATE["t"] + 2, PLATE["L"] / 2, PLATE["W"] / 2, -1))
    if case == "plate_hole_quicker":
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
        plate = _box(0, 0, 0, PLATE["L"], PLATE["W"], PLATE["t"])
        for k in range(int(RIBS["n"])):
            rib = _box(RIBS["x0"] + k * RIBS["pitch"], 0.1 * PLATE["W"], PLATE["t"], RIBS["w"], 0.8 * PLATE["W"], RIBS["h"])
            plate = BRepAlgoAPI_Fuse(plate, rib).Shape()
        return _cut(plate, _cyl(PLATE["d"] / 2, PLATE["t"] + RIBS["h"] + 2, PLATE["L"] / 2, PLATE["W"] / 2, -1))
    if case == "plate_hole_rounds_quicker":
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
        plate = _box(0, 0, 0, PLATE["L"], PLATE["W"], PLATE["t"])
        for k in range(int(RIBS["n"])):
            x0 = RIBS["x0"] + k * RIBS["pitch"]
            rib = _box(x0, 0.1 * PLATE["W"], PLATE["t"], RIBS["w"], 0.8 * PLATE["W"], RIBS["h"])
            rib = _round_edges(rib, RIB_ROUND, lambda a, b: min(a[2], b[2]) > PLATE["t"] + RIBS["h"] - 1e-6
                               and abs(a[1] - b[1]) > 1.0)
            plate = BRepAlgoAPI_Fuse(plate, rib).Shape()
        return _cut(plate, _cyl(PLATE["d"] / 2, PLATE["t"] + RIBS["h"] + 2, PLATE["L"] / 2, PLATE["W"] / 2, -1))
    if case in ("shaft_fillet", "shaft_fillet_quicker"):
        return _shaft()
    if case == "groove_quicker":
        bar = _cut(_cyl(GROOVE["D"] / 2, GROOVE["L"]), _torus(GROOVE["D"] / 2, GROOVE["r"], GROOVE["L"] / 2))
        bar = _round_circles(bar, [(GROOVE["D"] / 2, 0.0, SHAFT["round"]), (GROOVE["D"] / 2, GROOVE["L"], SHAFT["round"])])
        return _common(bar, _box(0, 0, -1, GROOVE["D"], GROOVE["D"], GROOVE["L"] + 2))
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
    if case == "spring":
        from tests import fea_fixtures as fx
        return fx.coil_spring()
    if case in ("modal", "buckling"):
        return _box(0, 0, 0, SLENDER["h"], SLENDER["h"], SLENDER["L"])
    if case == "bonded_corner_tie":
        from tests import fea_fixtures as fx
        return fx.corner_blocks(CORNER["a"])
    if case == "bonded_gap_tension":
        from tests import fea_fixtures as fx
        return fx.gapped_blocks(BLOCKS["a"], BLOCKS["gap"])
    if case == "pair_off_carries_nothing":
        from tests import fea_fixtures as fx
        return fx.off_and_bonded_blocks(BLOCKS["a"])
    if case in CONNECTION:
        from tests import fea_fixtures as fx
        return fx.touching_blocks(BLOCKS["a"])
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
    state = {"purpose": "structural", **({"fea_connection": CONNECTION[case]} if case in CONNECTION else {}),
             **({"fea_pairs": PAIRS[case]} if case in PAIRS else {})}
    if case in QUICKER_SHARE:
        # PLAN-fea-22: THE QUICKER MESH, its cap set to a share of this part's full-rules estimate (QUICKER_SHARE)
        from meshpipeline.engines import fea_plan as P
        from meshpipeline.engines import fea_plan_options as PO
        record, surface, diag_m = FD.measure_staged(ws)
        full = FD.choose_plan(record, surface, diag_m)["estimate"]["elements"]
        share = QUICKER_SHARE[case]
        monkeypatch.setattr(PO, "standard_elements", lambda: int(share * full / P.LIMIT_AIM))
        state["fea_plan_choice"] = "quicker"
    ok, marker, _o = asyncio.run(FD.drive(ws, state, job_id=f"acc-{case}", publish=None, run=_Run()))
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
             for m in re.finditer(r"\*NSET, NSET=((?:Part_\d+_)?Face_\d+)\n((?:[^*][^\n]*\n)*)", text)}
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


def _plate_quicker(ws, q):
    """The ribbed plate meshed quicker: only the ribs' capped corners were coarsened (the plan says), and the hole
    reads the plate's Kt - the stress at the ribs' sharp corners never settles, so the peak is read round the hole."""
    plan = json.loads((ws / "fea_plan.json").read_text())
    assert sorted({z["kind"] for z in plan["zones"] if z.get("short")}) == ["corner"], plan.get("resolution_short")
    deck, nodes, nsets = _deck(ws)
    fixed = _face(nodes, nsets, lambda p: abs(p[0]) < 1e-9)
    load = _face(nodes, nsets, lambda p: abs(p[0] - PLATE["L"] * 1e-3) < 1e-9)
    ids = nsets[fixed]
    a = _pick(nodes, ids, lambda p: p[1] + p[2])
    b = _pick(nodes, ids, lambda p: -p[1] + p[2])
    c = _pick(nodes, ids, lambda p: p[1] - p[2])
    gross = 1.0e6
    _dat, frd = _solve(ws, "ktq", deck, _material("STEEL", 0.0) + f"""*NSET, NSET=PA
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
    centre = np.array([PLATE["L"] / 2, PLATE["W"] / 2]) * 1e-3
    near = {n for n, p in nodes.items() if np.linalg.norm(p[:2] - centre) <= PLATE["d"] * 1e-3}
    smax = max(st[0] for n, st in _frd_stress(frd).items() if n in near)
    net = gross * PLATE["W"] / (PLATE["W"] - PLATE["d"])
    return smax / net, 2.0 + (1 - PLATE["d"] / PLATE["W"]) ** 3, "net-section Kt at the hole, quicker mesh"


def _quicker_kinds(ws) -> list[str]:
    """What the quicker mesh coarsened (the plan's short zones): checked to be the low-stress kinds only."""
    from meshpipeline.engines import fea_plan_options as PO
    plan = json.loads((ws / "fea_plan.json").read_text())
    kinds = sorted({z["kind"] for z in plan["zones"] if z.get("short")})
    assert kinds and set(kinds) <= PO.LOW_STRESS, plan.get("resolution_short")
    return kinds


def _plate_rounds_quicker(ws, q):
    """The ribbed plate, its ribs' tops rounded, meshed quicker: its rounds and capped corners coarsened (the plan
    says), and the hole reads the plate's Kt."""
    _quicker_kinds(ws)
    return _plate_quicker_read(ws, "ktr")


def _plate_quicker_read(ws, job):
    deck, nodes, nsets = _deck(ws)
    fixed = _face(nodes, nsets, lambda p: abs(p[0]) < 1e-9)
    load = _face(nodes, nsets, lambda p: abs(p[0] - PLATE["L"] * 1e-3) < 1e-9)
    ids = nsets[fixed]
    a = _pick(nodes, ids, lambda p: p[1] + p[2])
    b = _pick(nodes, ids, lambda p: -p[1] + p[2])
    c = _pick(nodes, ids, lambda p: p[1] - p[2])
    gross = 1.0e6
    _dat, frd = _solve(ws, job, deck, _material("STEEL", 0.0) + f"""*NSET, NSET=PA
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
    centre = np.array([PLATE["L"] / 2, PLATE["W"] / 2]) * 1e-3
    near = {n for n, p in nodes.items() if np.linalg.norm(p[:2] - centre) <= PLATE["d"] * 1e-3}
    smax = max(st[0] for n, st in _frd_stress(frd).items() if n in near)
    net = gross * PLATE["W"] / (PLATE["W"] - PLATE["d"])
    return smax / net, 2.0 + (1 - PLATE["d"] / PLATE["W"]) ** 3, "net-section Kt at the hole, quicker mesh"


def _axial_bar(ws, job: str, length: float, end_radius: float, net_radius: float, near_z: float, reach: float):
    """A quarter round bar pulled along z by 1 MPa on its flat top end, held axially on its flat bottom end and on
    its two symmetry planes: the largest axial stress within `reach` (m) of z = `near_z` over the mean axial stress
    in the section of radius `net_radius` (the flat end's force over it)."""
    deck, nodes, nsets = _deck(ws)
    L = length * 1e-3
    bot = _face(nodes, nsets, lambda x: abs(x[2]) < 1e-9)
    top = _face(nodes, nsets, lambda x: abs(x[2] - L) < 1e-9)
    sx = _face(nodes, nsets, lambda x: abs(x[0]) < 1e-9)
    sy = _face(nodes, nsets, lambda x: abs(x[1]) < 1e-9)
    gross = 1.0e6
    _dat, frd = _solve(ws, job, deck, _material("STEEL", 0.3) + f"""*BOUNDARY
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
    near = {n for n, p in nodes.items() if abs(p[2] - near_z * 1e-3) <= reach}
    smax = max(st[2] for n, st in _frd_stress(frd).items() if n in near)
    return smax / (gross * (end_radius / net_radius) ** 2)


def _shaft_read(ws, q, job):
    got = _axial_bar(ws, job, SHAFT["L"], SHAFT["D"] / 2 - SHAFT["round"], SHAFT["d"] / 2, SHAFT["L"] / 2,
                     2 * SHAFT["r"] * 1e-3)
    return got, SHAFT["kt"], "shoulder fillet Kt (converged axisymmetric reference)"


def _shaft_full(ws, q):
    return _shaft_read(ws, q, "shaft")


def _shaft_quicker(ws, q):
    _quicker_kinds(ws)
    got, want, what = _shaft_read(ws, q, "shaftq")
    return got, want, what + ", quicker mesh"


def _groove_quicker(ws, q):
    _quicker_kinds(ws)
    got = _axial_bar(ws, "grooveq", GROOVE["L"], GROOVE["D"] / 2 - SHAFT["round"], GROOVE["D"] / 2 - GROOVE["r"],
                     GROOVE["L"] / 2, 2 * GROOVE["r"] * 1e-3)
    return got, GROOVE["kt"], "net-section Kt (converged axisymmetric reference), quicker mesh"


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


def _spring(ws, q):
    """A HELICAL COIL SPRING under an axial load: the peak shear in the wire against the Wahl-corrected
    formula tau = K_w 8 P D / (pi d^3), K_w = (4C - 1)/(4C - 4) + 0.615/C, C = D/d - read away from the
    two loaded ends (a coil's length off each), where the end loads spread into the wire. PLAN-fea-7
    gives the round wire its 16 elements round (22 Oct: without it the wire meshed as a ribbon)."""
    from tests.fea_fixtures import SPRING

    deck, nodes, nsets = _deck(ws)
    D, d, pitch, turns = (SPRING[k] * (1e-3 if k != "turns" else 1.0) for k in ("D", "d", "pitch", "turns"))
    start = np.array([D / 2, 0.0, 0.0])
    end = np.array([D / 2, 0.0, pitch * turns])
    fixed = _face(nodes, nsets, lambda x: np.linalg.norm(x - start) < 0.51 * d)
    loaded = _face(nodes, nsets, lambda x: np.linalg.norm(x - end) < 0.51 * d)
    # 100 N ALONG THE COIL'S AXIS, as a spring is loaded: the loaded end is a rigid body led by a
    # node on the axis (a force at the wire's end, off the axis, also bends the whole coil sideways)
    force = 100.0
    ref, rot = 9_000_001, 9_000_002
    _dat, frd = _solve(ws, "spring", deck, _material("STEEL", 0.3) + f"""*NODE
{ref}, 0.0, 0.0, {end[2]:.9e}
{rot}, 0.0, 0.0, {end[2]:.9e}
*RIGID BODY, NSET={loaded}, REF NODE={ref}, ROT NODE={rot}
*BOUNDARY
{fixed}, 1, 3, 0.0
{ref}, 1, 2, 0.0
{rot}, 1, 3, 0.0
*STEP
*STATIC, SOLVER=ITERATIVE CHOLESKY
*CLOAD
{ref}, 3, {force:.6e}
*NODE FILE
S
*END STEP
""")
    st = _frd_stress(frd)
    peak = 0.0
    for nid, (sxx, syy, szz, sxy, syz, szx) in st.items():
        if not (pitch < nodes[nid][2] < pitch * (turns - 1)):
            continue
        ev = np.linalg.eigvalsh(np.array([[sxx, sxy, szx], [sxy, syy, syz], [szx, syz, szz]]))
        peak = max(peak, 0.5 * float(ev[-1] - ev[0]))
    c = D / d
    k_w = (4 * c - 1) / (4 * c - 4) + 0.615 / c
    return peak, k_w * 8 * force * D / (math.pi * d ** 3), "peak wire shear (Wahl-corrected)"


def _faces(nodes, nsets, test) -> list[str]:
    """Every face all of whose nodes pass `test(xyz)` (an assembly has one per part on a plane)."""
    return [n for n, ids in nsets.items() if all(test(nodes[i]) for i in ids)]


def _blocks(ws, sign: float, contact: bool, gap: float = 0.0):
    """The two blocks held on x = 0 (rollers on y = 0 and z = 0, so Poisson's ratio adds no
    constraint) and loaded by BLOCKS["p"] on x = 2a (+ the gap between them, mm): sign -1 pulls
    (tension), +1 pushes."""
    deck, nodes, nsets = _deck(ws)
    a = BLOCKS["a"] * 1e-3
    fixed = _face(nodes, nsets, lambda q: abs(q[0]) < 1e-9)
    load = _face(nodes, nsets, lambda q: abs(q[0] - 2 * a - gap * 1e-3) < 1e-9)
    y0 = _faces(nodes, nsets, lambda q: abs(q[1]) < 1e-9)
    z0 = _faces(nodes, nsets, lambda q: abs(q[2]) < 1e-9)
    assert len(y0) == 2 and len(z0) == 2, (y0, z0)
    solver = "" if contact else ", SOLVER=ITERATIVE CHOLESKY"
    bc = "\n".join([f"{fixed}, 1, 1, 0.0"] + [f"{f}, 2, 2, 0.0" for f in y0] + [f"{f}, 3, 3, 0.0" for f in z0])
    _dat, frd = _solve(ws, "blk", deck, _material("STEEL", 0.3) + f"""*BOUNDARY
{bc}
*STEP
*STATIC{solver}
*DSLOAD
{load}, P, {sign * BLOCKS["p"]:.6e}
*NODE FILE
U, S
*END STEP
""")
    return nodes, nsets, load, _frd_disp(frd), _frd_stress(frd)


def _bonded_tension(ws, q):
    nodes, nsets, load, u, st = _blocks(ws, -1.0, contact=False)
    # THE JOINT IS INVISIBLE: sigma_xx is p everywhere, on both sides of the shared face
    sxx = np.array([v[0] for v in st.values()])
    assert np.max(np.abs(sxx / BLOCKS["p"] - 1.0)) < 0.01, f"sigma_xx {sxx.min():.4g}..{sxx.max():.4g}"
    end = float(np.mean([u[i][0] for i in nsets[load]]))
    return end, BLOCKS["p"] * 2 * BLOCKS["a"] * 1e-3 / E, "end displacement (m), as one bar"


def _bonded_gap_tension(ws, q):
    """PLAN-fea-18: two blocks 0.2 mm apart, bonded - the small-gap joint is a *TIE whose reach spans
    the gap, so every node of the joint is tied and the pull crosses it whole: sigma_xx is p on both
    sides, and the far end moves as one bar's would (the air in the gap carries no strain). A tie that
    missed nodes would leave the second block loose (CalculiX stops: no solution) or the stress uneven."""
    deck = (ws / "mesh.inp").read_text()
    assert "POSITION TOLERANCE=" in deck and "ADJUST=NO" in deck, "the gap joint's reach is not in the deck"
    nodes, nsets, load, u, st = _blocks(ws, -1.0, contact=False, gap=BLOCKS["gap"])
    sxx = np.array([v[0] for v in st.values()])
    assert np.max(np.abs(sxx / BLOCKS["p"] - 1.0)) < 0.02, f"sigma_xx {sxx.min():.4g}..{sxx.max():.4g}"
    end = float(np.mean([u[i][0] for i in nsets[load]]))
    return end, BLOCKS["p"] * 2 * BLOCKS["a"] * 1e-3 / E, "end displacement (m), as one bar across the gap"


def _bonded_corner_tie(ws, q):
    """A bonded mesh whose joint the measurement also finds as a line contact (tests/fea_fixtures.corner_blocks):
    Part_2 and Part_3 meet along a line the fused mesh already gives them both, so every node near it is
    both parts' own. Tied as well, CalculiX stopped ("zero coefficient on the dependent side": a node tied
    to a face it lies on); settled (settle_ties), it is a bonded joint and no *TIE is written. Held on x = 0
    (rollers on y = 0 and z = 0), Part_2's end pulled by p: CalculiX solves with no error, the reaction
    balances the pull to 0.1%, and Part_2's pulled end - a plain bar a section's length past its joint -
    carries sigma_xx = p."""
    deck, nodes, nsets = _deck(ws)
    assert "*TIE" not in deck, "parts that share their nodes are tied as well"
    assert "Bond_3_Part_2" in deck, "the line contact is not named as a bonded joint"
    a = CORNER["a"] * 1e-3
    fixed = _face(nodes, nsets, lambda x: abs(x[0]) < 1e-9)
    load = _face(nodes, nsets, lambda x: abs(x[0] - 6 * a) < 1e-9)
    y0 = _faces(nodes, nsets, lambda x: abs(x[1]) < 1e-9)
    z0 = _faces(nodes, nsets, lambda x: abs(x[2]) < 1e-9)
    assert len(y0) == 2 and len(z0) == 3, (y0, z0)
    bc = "\n".join([f"{fixed}, 1, 1, 0.0"] + [f"{f}, 2, 2, 0.0" for f in y0] + [f"{f}, 3, 3, 0.0" for f in z0])
    dat, frd = _solve(ws, "corner", deck, _material("STEEL", 0.3) + f"""*BOUNDARY
{bc}
*STEP
*STATIC, SOLVER=ITERATIVE CHOLESKY
*DSLOAD
{load}, P, {-BLOCKS["p"]:.6e}
*NODE FILE
U, S
*NODE PRINT, NSET={fixed}, TOTALS=ONLY
RF
*END STEP
""")
    pull = BLOCKS["p"] * a * a
    assert abs(-_total(dat, 0) / pull - 1.0) < 1e-3, f"reaction {_total(dat, 0):.6g} N against a pull of {pull:.6g} N"
    st = _frd_stress(frd)
    far = [st[i][0] for i in nsets[load] if i in st]
    worst = float(max(far, key=lambda s: abs(s / BLOCKS["p"] - 1.0)))
    return worst, BLOCKS["p"], "sigma_xx at the pulled end (Pa), worst node"


def _pair_off_carries_nothing(ws, q):
    """PLAN-fea-19: three 100 mm blocks - A held on x = 0, B bonded to A's far end, C sitting on A's top
    face with its pair set OFF (and held on its own faces, so it cannot drift). B is pulled by p: the
    bonded pair carries the whole load (sigma_xx = p in B, and A and B stretch as one 200 mm bar), and
    the Off pair carries none (C stays unstressed: no tie, no contact, nothing joins it to A)."""
    deck, nodes, nsets = _deck(ws)
    assert deck.count("*TIE, NAME=") == 1 and "*CONTACT PAIR" not in deck, "only A-B may be joined"
    a = BLOCKS["a"] * 1e-3
    load = _face(nodes, nsets, lambda q: abs(q[0] - 2 * a) < 1e-9)
    x0 = _faces(nodes, nsets, lambda q: abs(q[0]) < 1e-9)                   # A's and C's
    y0 = _faces(nodes, nsets, lambda q: abs(q[1]) < 1e-9)                   # A's and B's
    z0 = _faces(nodes, nsets, lambda q: abs(q[2]) < 1e-9)                   # all three
    top = _face(nodes, nsets, lambda q: abs(q[1] - 2 * a) < 1e-9)           # C's own top
    assert len(x0) == 2 and len(y0) == 2 and len(z0) == 3, (x0, y0, z0)
    bc = "\n".join([f"{f}, 1, 1, 0.0" for f in x0] + [f"{f}, 2, 2, 0.0" for f in [*y0, top]]
                   + [f"{f}, 3, 3, 0.0" for f in z0])
    _dat, frd = _solve(ws, "off", deck, _material("STEEL", 0.3) + f"""*BOUNDARY
{bc}
*STEP
*STATIC, SOLVER=ITERATIVE CHOLESKY
*DSLOAD
{load}, P, {-BLOCKS["p"]:.6e}
*NODE FILE
U, S
*END STEP
""")
    u, st = _frd_disp(frd), _frd_stress(frd)
    in_b = [i for i, x in nodes.items() if x[0] > a * 1.001 and i in st]
    in_c = [i for i, x in nodes.items() if x[1] > a * 1.001 and i in st]
    sxx_b = np.array([st[i][0] for i in in_b])
    assert np.max(np.abs(sxx_b / BLOCKS["p"] - 1.0)) < 0.02, f"B: sigma_xx {sxx_b.min():.4g}..{sxx_b.max():.4g}"
    # THE OFF PAIR CARRIES NOTHING: C is unstressed (to the solver's round-off)
    c_peak = max(float(np.max(np.abs(st[i]))) for i in in_c)
    assert c_peak < 1e-6 * BLOCKS["p"], f"C carries {c_peak:.4g} Pa through a pair set Off"
    end = float(np.mean([u[i][0] for i in nsets[load]]))
    return end, BLOCKS["p"] * 2 * BLOCKS["a"] * 1e-3 / E, "end displacement (m): A and B as one bar, C carrying nothing"


def _contact_compression(ws, q):
    _nodes, _nsets, _load, _u, st = _blocks(ws, 1.0, contact=True)
    sxx = np.array([v[0] for v in st.values()])
    # every node in compression at p: the load crosses the contact pair whole
    worst = float(sxx[np.argmax(np.abs(sxx + BLOCKS["p"]))])
    return worst, -BLOCKS["p"], "sigma_xx across the contact (Pa), worst node"


CASES = {
    "plate_hole": ("static", _plate, 0.03),
    "plate_hole_quicker": ("static", _plate_quicker, 0.03),
    "plate_hole_rounds_quicker": ("static", _plate_rounds_quicker, 0.03),
    "shaft_fillet": ("static", _shaft_full, 0.03),
    "shaft_fillet_quicker": ("static", _shaft_quicker, 0.03),
    "groove_quicker": ("static", _groove_quicker, 0.03),
    "cantilever": ("static", _cantilever, 0.01),
    "lame": ("static", _lame, 0.03),
    "groove": ("static", _groove, 0.03),
    "modal": ("modal", _modal, 0.01),
    "buckling": ("buckling", _buckling, 0.01),
    # the Wahl factor is itself a fit (within about 2% of the exact coil solution at this index)
    "spring": ("static", _spring, 0.05),
    "bonded_tension": ("static", _bonded_tension, 0.01),
    "contact_compression": ("static", _contact_compression, 0.01),
    "bonded_gap_tension": ("static", _bonded_gap_tension, 0.01),
    "bonded_corner_tie": ("static", _bonded_corner_tie, 0.01),
    "pair_off_carries_nothing": ("static", _pair_off_carries_nothing, 0.01),
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
