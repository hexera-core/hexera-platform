# Responsibility: Prove in CalculiX that the shell-to-solid join (engines/shell_links.py, SHLINK-1..2) carries
# bending moments: a half-shell, half-solid cantilever against the pure shell and PL^3/3EI, a plain shared-node join
# shown to be a hinge, and a T-joint in bending (web shell on a solid plate; plate shell under a solid web) against
# the all-solid T.
# Boundaries: needs CalculiX (as test_fea_textbook.py: HEXERA_CCX, or ccx on the path; CI's accuracy lane runs it).
# Meshes are structured here (S4 and C3D20R on known grids) so only the join is under test.
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from meshpipeline.engines import shell_links as sl

CCX = os.environ.get("HEXERA_CCX") or shutil.which("ccx") or ""
REQUIRE_CCX = os.environ.get("FEA_ACCURACY_REQUIRE_CCX", "") == "1"
pytestmark = pytest.mark.skipif(not REQUIRE_CCX and not (CCX and Path(CCX).exists()),
                                reason="CalculiX (ccx) not installed - set HEXERA_CCX to run")
E, NU = 210000.0, 0.3
#: CalculiX expands S4R into one layer of C3D8R, which has no bending stiffness to speak of (a 200 mm cantilever
#: bent 10 times too far); S4 expands into C3D8I (incompatible modes) and is right. The CalculiX deck uses S4.
SHELL = "S4"


class Deck:
    def __init__(self):
        self.key: dict[tuple, int] = {}
        self.xyz: list[tuple[float, float, float]] = []
        self.shell: list[tuple[list[int], float]] = []
        self.solid: list[list[int]] = []
        self.extra: list[str] = []

    def node(self, x, y, z, *, own: bool = False) -> int:
        k = (round(float(x), 6), round(float(y), 6), round(float(z), 6))
        if own:
            self.xyz.append(k)
            return len(self.xyz)
        if k not in self.key:
            self.xyz.append(k)
            self.key[k] = len(self.xyz)
        return self.key[k]

    def box(self, xs, ys, zs):
        for xa, xb in zip(xs[:-1], xs[1:]):
            for ya, yb in zip(ys[:-1], ys[1:]):
                for za, zb in zip(zs[:-1], zs[1:]):
                    xm, ym, zm = (xa + xb) / 2, (ya + yb) / 2, (za + zb) / 2
                    pts = [(xa, ya, za), (xb, ya, za), (xb, yb, za), (xa, yb, za),
                           (xa, ya, zb), (xb, ya, zb), (xb, yb, zb), (xa, yb, zb),
                           (xm, ya, za), (xb, ym, za), (xm, yb, za), (xa, ym, za),
                           (xm, ya, zb), (xb, ym, zb), (xm, yb, zb), (xa, ym, zb),
                           (xa, ya, zm), (xb, ya, zm), (xb, yb, zm), (xa, yb, zm)]
                    self.solid.append([self.node(*p) for p in pts])

    def sheet(self, us, vs, at, t, node=None):
        node = node or (lambda u, v: self.node(*at(u, v)))
        for ua, ub in zip(us[:-1], us[1:]):
            for va, vb in zip(vs[:-1], vs[1:]):
                self.shell.append(([node(ua, va), node(ub, va), node(ub, vb), node(ua, vb)], t))

    def solve(self, ws: Path, job: str, fixed, loaded, force: tuple[int, float], watch) -> np.ndarray:
        out = ["*NODE, NSET=NALL"] + [f"{i + 1}, {x}, {y}, {z}" for i, (x, y, z) in enumerate(self.xyz)]
        n = 0
        walls: dict[float, list[int]] = {}
        if self.shell:
            out.append(f"*ELEMENT, TYPE={SHELL}, ELSET=SH")
            for conn, t in self.shell:
                n += 1
                out.append(f"{n}, " + ", ".join(map(str, conn)))
                walls.setdefault(t, []).append(n)
        if self.solid:
            out.append("*ELEMENT, TYPE=C3D20R, ELSET=SO")
            for conn in self.solid:
                n += 1
                vals = [n, *conn]
                out += [", ".join(map(str, vals[i:i + 15])) + ("," if i + 15 < len(vals) else "")
                        for i in range(0, len(vals), 15)]
        out += ["*MATERIAL, NAME=STEEL", "*ELASTIC", f"{E}, {NU}"]
        for k, (t, ids) in enumerate(sorted(walls.items())):
            out += [f"*ELSET, ELSET=W{k}"] + [", ".join(map(str, ids[i:i + 12])) for i in range(0, len(ids), 12)]
            out += [f"*SHELL SECTION, ELSET=W{k}, MATERIAL=STEEL", f"{t}"]
        if self.solid:
            out += ["*SOLID SECTION, ELSET=SO, MATERIAL=STEEL"]
        out += self.extra
        ids = {name: [i + 1 for i, p in enumerate(self.xyz) if test(p)] for name, test in
               (("FIX", fixed), ("LOAD", loaded), ("WATCH", watch))}
        for name, nodes in ids.items():
            assert nodes, name
            out += [f"*NSET, NSET={name}"] + [", ".join(map(str, nodes[i:i + 12])) for i in range(0, len(nodes), 12)]
        shell_nodes = {k for conn, _t in self.shell for k in conn}
        out += ["*BOUNDARY", "FIX, 1, 3"]
        if any(i in shell_nodes for i in ids["FIX"]):
            out += [f"{i}, 4, 6" for i in ids["FIX"] if i in shell_nodes]
        dof, total = force
        out += ["*STEP", "*STATIC", "*CLOAD"] + [f"{i}, {dof}, {total / len(ids['LOAD'])}" for i in ids["LOAD"]]
        out += ["*NODE PRINT, NSET=WATCH", "U", "*END STEP"]
        (ws / f"{job}.inp").write_text("\n".join(out) + "\n")
        env = {**os.environ, "OMP_NUM_THREADS": "1", "CCX_NPROC_EQUATION_SOLVER": "1"}
        proc = subprocess.run([CCX, job], cwd=ws, capture_output=True, text=True, timeout=1800, env=env)
        assert proc.returncode == 0 and "*ERROR" not in proc.stdout, proc.stdout[-2000:]
        rows = [ln.split() for ln in (ws / f"{job}.dat").read_text().splitlines()]
        return np.array([[float(v) for v in r[1:4]] for r in rows if len(r) == 4 and r[0].isdigit()])


# ------------------------------------------------------------------------------------ the cantilever ----
L, B, T, P = 200.0, 20.0, 2.0, 10.0
NX, NY = 40, 4
H = L / NX
BEAM = P * L ** 3 / (3 * E * B * T ** 3 / 12)
XS, YS = np.linspace(0, L, NX + 1), np.linspace(0, B, NY + 1)


def _tip(d: Deck, ws: Path, job: str) -> float:
    u = d.solve(ws, job, lambda p: abs(p[0]) < 1e-9, lambda p: abs(p[0] - L) < 1e-9, (3, P),
                lambda p: abs(p[0] - L) < 1e-9 and abs(p[2]) < 1e-9)
    return float(u[:, 2].mean())


def _cantilever(overlap: int, *, tied: bool) -> Deck:
    """Solid root half (2 C3D20R through the wall), shell tip half; the shell runs `overlap` elements into the
    solid, its nodes there its own and band-tied (tied) or the solid's own (shared)."""
    d = Deck()
    half = L / 2
    d.box(XS[XS <= half + 1e-9], YS, [-T / 2, 0.0, T / 2])
    x0 = half - overlap * H
    us = XS[XS >= x0 - 1e-9]
    own: dict[tuple, int] = {}

    def node(u, v):
        if tied and u <= half + 1e-9:
            k = (round(u, 6), round(v, 6))
            if k not in own:
                own[k] = d.node(u, v, 0.0, own=True)
            return own[k]
        return d.node(u, v, 0.0)

    d.sheet(us, YS, None, T, node=node)
    if tied:
        solid = {i + 1: np.array(p) for i, p in enumerate(d.xyz) if (i + 1) not in own.values()}
        nodes = {k: np.array(d.xyz[k - 1]) for k in own.values()}
        ties = sl.band_ties(nodes, {k: np.array([0, 0, 1.0]) for k in nodes}, dict.fromkeys(nodes, T), solid,
                            cap=sl.TIE_REACH * H)
        assert {t.node for t in ties} == set(nodes)
        d.extra += sl.equations(ties)
    return d


def test_a_shell_cantilever_bends_as_beam_theory_says(tmp_path):
    d = Deck()
    d.sheet(XS, YS, lambda u, v: (u, v, 0.0), T)
    assert _tip(d, tmp_path, "shell") == pytest.approx(BEAM, rel=0.02)


def test_a_half_shell_half_solid_cantilever_carries_the_moment(tmp_path):
    pure = Deck()
    pure.sheet(XS, YS, lambda u, v: (u, v, 0.0), T)
    shell = _tip(pure, tmp_path, "pure")
    mixed = _tip(_cantilever(1, tied=True), tmp_path, "mixed")
    assert mixed == pytest.approx(shell, rel=0.02)
    assert mixed == pytest.approx(BEAM, rel=0.02)


def test_shell_and_solid_sharing_only_their_meeting_line_is_a_hinge(tmp_path):
    hinge = _tip(_cantilever(0, tied=False), tmp_path, "hinge")
    assert abs(hinge) > 100 * BEAM


# ------------------------------------------------------------------------------------- the T-joint ----
TP, TW, HW, F = 4.0, 3.0, 50.0, 100.0
TX = np.unique(np.concatenate([np.linspace(0, 45, 10), [47.0, 48.5, 50.0, 51.5, 53.0], np.linspace(55, 100, 10)]))
TY = np.linspace(0, 40, 9)
TZ = np.linspace(TP / 2, TP / 2 + HW, 11)
WEB_X = [50 - TW / 2, 50.0, 50 + TW / 2]


def _t_top(d: Deck, ws: Path, job: str) -> float:
    top = TP / 2 + HW
    u = d.solve(ws, job, lambda p: p[0] < 1e-9 or p[0] > 100 - 1e-9, lambda p: abs(p[2] - top) < 1e-9, (1, F),
                lambda p: abs(p[2] - top) < 1e-9)
    return float(u[:, 0].mean())


def _t_reference(ws: Path) -> tuple[float, float]:
    solid = Deck()
    solid.box(TX, TY, [-TP / 2, 0.0, TP / 2])
    solid.box(WEB_X, TY, TZ)
    shell = Deck()
    shell.sheet(TX, TY, lambda u, v: (u, v, 0.0), TP)
    shell.sheet(np.concatenate([[0.0], TZ]), TY, lambda u, v: (50.0, v, u), TW)
    return _t_top(solid, ws, "t_solid"), _t_top(shell, ws, "t_shell")


def test_a_t_joint_carries_its_bending_moment_across_both_links(tmp_path):
    ref_solid, ref_shell = _t_reference(tmp_path)
    # SHLINK-1: the web shell runs into the solid plate down to its mid-plane
    d = Deck()
    d.box(TX, TY, [-TP / 2, 0.0, TP / 2])
    own: dict[tuple, int] = {}

    def node(u, v):
        if u <= TP / 2 + 1e-9:
            k = (round(u, 6), round(v, 6))
            if k not in own:
                own[k] = d.node(50.0, v, u, own=True)
            return own[k]
        return d.node(50.0, v, u)

    d.sheet(np.concatenate([[0.0], TZ]), TY, None, TW, node=node)
    solid = {i + 1: np.array(p) for i, p in enumerate(d.xyz) if (i + 1) not in own.values()}
    nodes = {k: np.array(d.xyz[k - 1]) for k in own.values()}
    ties = sl.band_ties(nodes, {k: np.array([1.0, 0, 0]) for k in nodes}, dict.fromkeys(nodes, TW), solid,
                        cap=sl.TIE_REACH * (TZ[1] - TZ[0]))
    d.extra += sl.equations(ties)
    web = _t_top(d, tmp_path, "t_webshell")
    assert web == pytest.approx(ref_shell, rel=0.02)
    assert web == pytest.approx(ref_solid, rel=0.06)
    # SHLINK-2: the plate shell runs under a solid web standing on its top face
    d = Deck()
    d.sheet(TX, TY, lambda u, v: (u, v, 0.0), TP)
    first = len(d.xyz)
    d.box(WEB_X, TY, TZ)
    face = {i + 1: np.array(p) for i, p in enumerate(d.xyz) if i >= first and abs(p[2] - TP / 2) < 1e-9}
    feet, offset = {}, {}
    for i, p in face.items():
        kx = [(x, w) for x, w in _hat(TX, p[0])]
        ky = [(y, w) for y, w in _hat(TY, p[1])]
        feet[i] = [(d.key[(round(x, 6), round(y, 6), 0.0)], wx * wy) for x, wx in kx for y, wy in ky]
        offset[i] = np.array([0.0, 0.0, TP / 2])
    d.extra += sl.equations(sl.face_ties(face, feet, offset))
    plate = _t_top(d, tmp_path, "t_plateshell")
    assert plate == pytest.approx(ref_solid, rel=0.05)


def _hat(grid: np.ndarray, x: float) -> list[tuple[float, float]]:
    """Linear shape functions of a 1D grid at x: [(grid point, weight)]."""
    k = int(np.searchsorted(grid, x - 1e-9))
    if abs(grid[k] - x) < 1e-9:
        return [(float(grid[k]), 1.0)]
    a, b = float(grid[k - 1]), float(grid[k])
    return [(a, (b - x) / (b - a)), (b, (x - a) / (b - a))]


# ----------------------------------------------------------- a short plate as a shell (SHELL-23's advice) ----
#: a gusset-sized plate: 12.7 mm thick, 96.5 mm long and wide - 7.6 walls across, under SHELL-2's ten
SHORT_T, SHORT_WALLS = 12.7, 7.6
SHORT_L = SHORT_T * SHORT_WALLS
#: the shell's tip against the solid's. Measured (10 Nov, CalculiX 2.21, these meshes): -0.05% at 7.6 walls, -0.05%
#: at 5, -0.30% at 3, -0.08% at 10 - a plate alone bends the same as a shell and as a solid, however short
SHORT_OFF_MAX = 0.01


def _short_tip(d: Deck, ws: Path, job: str) -> float:
    u = d.solve(ws, job, lambda p: abs(p[0]) < 1e-9, lambda p: abs(p[0] - SHORT_L) < 1e-9, (3, 1000.0),
                lambda p: abs(p[0] - SHORT_L) < 1e-9 and abs(p[2]) < 1e-9)
    return float(u[:, 2].mean())


def test_a_short_plate_meshed_as_a_shell_bends_as_its_solid_does(tmp_path):
    """SHELL-23: a plate 7.6 walls long is a shell because the user asked. Clamped at one end and pushed at the
    other, its S4 shell model's tip moves within 1% of the C3D20R solid's (four through the wall; measured -0.05%),
    and both lie between the wide beam with shear (its sides held from curling) and the slender narrow beam: on a
    plate alone, short costs next to nothing - the cost is where a short wall bends from a joint (MIXED-8)."""
    n = 12
    xs, ys = np.linspace(0, SHORT_L, n + 1), np.linspace(0, SHORT_L, n + 1)
    shell = Deck()
    shell.sheet(xs, ys, lambda u, v: (u, v, 0.0), SHORT_T)
    solid = Deck()
    solid.box(xs, ys, np.linspace(-SHORT_T / 2, SHORT_T / 2, 5))
    w_shell, w_solid = _short_tip(shell, tmp_path, "short_shell"), _short_tip(solid, tmp_path, "short_solid")
    assert w_shell > 0 and w_solid > 0
    assert abs(w_shell / w_solid - 1.0) <= SHORT_OFF_MAX, (w_shell, w_solid)
    # the beam, wide (its sides held from curling: E / (1 - nu^2)), with its shear deflection
    inertia, area, g = SHORT_L * SHORT_T ** 3 / 12, SHORT_L * SHORT_T, E / (2 * (1 + NU))
    beam = 1000.0 * SHORT_L ** 3 / (3 * E / (1 - NU ** 2) * inertia) + 1000.0 * SHORT_L / (5 / 6 * g * area)
    slender = 1000.0 * SHORT_L ** 3 / (3 * E * inertia)
    assert beam <= w_solid <= slender, (w_solid, beam, slender)
