# Responsibility: Prove in CalculiX that a MIXED mesh (thin walls as shell, thick regions as solid, joined by SHLINK-4)
# carries load and moment as the part does: a 2 mm cantilever strip built into a solid block (the strip shell, the
# block and a two-wall stub solid) against beam theory and against the FEA path's own all-solid mesh, in bending
# out of plane and in plane; and a plate with a thick boss, free-free modes 7 to 12 and a static bend through the
# boss, against the all-solid mesh. The whole product path: extraction, split, shell mesh, the FEA path's solid
# region, the join, the CalculiX deck (engines/mixed_run.build, engines/mixed_mesh.write_inp).
# Boundaries: needs CalculiX (HEXERA_CCX, or ccx on the path; CI's accuracy lane runs it). mm, N, MPa. CalculiX on
# one thread (its eigen solver on several gave a spurious eigenvalue once).
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

CCX = os.environ.get("HEXERA_CCX") or shutil.which("ccx") or ""
REQUIRE_CCX = os.environ.get("FEA_ACCURACY_REQUIRE_CCX", "") == "1"
pytestmark = pytest.mark.skipif(not REQUIRE_CCX and not (CCX and Path(CCX).exists()),
                                reason="CalculiX (ccx) not installed - set HEXERA_CCX to run")
E, NU, RHO = 210000.0, 0.3, 7.85e-9
MAT = ["*ELASTIC", f"{E}, {NU}", "*DENSITY", f"{RHO}"]
L, B, T, BLK, P = 200.0, 20.0, 2.0, 20.0, 2.0


def _cantilever():
    from tests.fea_fixtures import _box, _fuse

    return _fuse(_box(-BLK, 0, -BLK / 2, BLK, B, BLK), _box(0, 0, -T / 2, L, B, T))


def _plate_boss():
    from tests.fea_fixtures import _box, _cyl, _fuse

    return _fuse(_box(0, 0, 0, 120, 80, 3), _cyl(80, 40, 3, 10, 20))


def _mixed(shape, ws: Path):
    from tests import fea_fixtures as fx

    from meshpipeline.engines import mixed_mesh as mm
    from meshpipeline.engines import mixed_run as mr

    step = fx.write_step(shape, ws / "part.step")
    rec: dict = {}
    m = mr.build(step, ws / "mixed", mm_per_unit=1.0, rec=rec)
    mm.write_inp(ws / "mixed_mesh.inp", m, units="mm",
                 materials=dict.fromkeys(range(1, len(set(m.shell.part)) + 1), MAT))
    xyz = np.vstack([m.shell.xyz, m.solid.xyz])
    return (ws / "mixed_mesh.inp").read_text(), xyz, set(range(1, len(m.shell.xyz) + 1)), m


def _solid(shape, ws: Path):
    from meshpipeline.engines import mixed_run as mr

    ws.mkdir(parents=True, exist_ok=True)
    sp, _q = mr._solid_mesh(shape, ws)
    lines = ["*NODE, NSET=NALL"] + [f"{k + 1}, {x:.12g}, {y:.12g}, {z:.12g}" for k, (x, y, z) in enumerate(sp.xyz.tolist())]
    lines.append("*ELEMENT, TYPE=C3D10, ELSET=SO")
    lines += [f"{k + 1}, " + ", ".join(str(int(v)) for v in row) for k, row in enumerate(sp.tets)]
    lines += ["*MATERIAL, NAME=MAT_1", *MAT, "*SOLID SECTION, ELSET=SO, MATERIAL=MAT_1"]
    return "\n".join(lines) + "\n", sp.xyz, set(), sp


def _ids(name, ids):
    ids = [int(v) for v in ids]
    return [f"*NSET, NSET={name}"] + [", ".join(map(str, ids[i:i + 16])) for i in range(0, len(ids), 16)]


def _run(ws: Path, job: str, deck: str) -> str:
    (ws / f"{job}.inp").write_text(deck)
    env = {**os.environ, "OMP_NUM_THREADS": "1", "CCX_NPROC_EQUATION_SOLVER": "1"}
    p = subprocess.run([CCX, job], cwd=ws, capture_output=True, text=True, timeout=3600, env=env)
    out = p.stdout + p.stderr
    assert p.returncode == 0 and "*ERROR" not in out, out[-2000:]
    return (ws / f"{job}.dat").read_text()


def _static(ws, job, model, fix, load, force, watch) -> np.ndarray:
    deck, xyz, shell_nodes, _m = model
    sel = {k: np.flatnonzero(t(xyz)) + 1 for k, t in (("FIX", fix), ("LOAD", load), ("WATCH", watch))}
    for k, v in sel.items():
        assert len(v), k
    text = deck + "\n".join(
        _ids("FIX", sel["FIX"]) + _ids("LOAD", sel["LOAD"]) + _ids("WATCH", sel["WATCH"])
        + ["*BOUNDARY", "FIX, 1, 3"] + [f"{i}, 4, 6" for i in sel["FIX"].tolist() if i in shell_nodes]
        + ["*STEP", "*STATIC", "*CLOAD"]
        + [f"{i}, {d + 1}, {force[d] / len(sel['LOAD']):.9g}" for i in sel["LOAD"].tolist() for d in range(3) if force[d]]
        + ["*NODE PRINT, NSET=WATCH", "U", "*END STEP"]) + "\n"
    dat = _run(ws, job, text)
    rows = [ln.split() for ln in dat.splitlines()]
    return np.array([[float(v) for v in r[1:4]] for r in rows if len(r) == 4 and r[0].isdigit()]).mean(axis=0)


def _modes(ws, job, model) -> list[float]:
    dat = _run(ws, job, model[0] + "*STEP\n*FREQUENCY\n12\n*END STEP\n")
    block = dat.split("E I G E N V A L U E   O U T P U T", 1)[1]
    f = []
    for ln in block.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$", ln)
        if m:
            f.append(float(m.group(4)))
        elif f and not ln.strip():
            break
    return f


@pytest.fixture(scope="module")
def cantilever(tmp_path_factory):
    ws = tmp_path_factory.mktemp("mixed_cantilever")
    return ws, _mixed(_cantilever(), ws), _solid(_cantilever(), ws / "solid")


def _tip(xyz):
    return np.abs(xyz[:, 0] - L) < 1e-6


def _back(xyz):
    return np.abs(xyz[:, 0] + BLK) < 1e-6


def test_a_shell_strip_built_into_a_solid_block_bends_as_beam_theory_says(cantilever):
    ws, mixed, solid = cantilever
    assert mixed[3].counts["links"] == 1 and len(mixed[3].solid.tets) < len(solid[3].tets)
    beam = P * L ** 3 / (3 * E * B * T ** 3 / 12)
    got = _static(ws, "mixed_z", mixed, _back, _tip, (0.0, 0.0, P), _tip)[2]
    ref = _static(ws, "solid_z", solid, _back, _tip, (0.0, 0.0, P), _tip)[2]
    assert got == pytest.approx(ref, rel=0.03)
    # the strip is ten walls wide: between beam theory (E) and a plate strip (E / (1 - nu^2)); built into a block
    # that gives a little at the root
    assert beam * (1 - NU ** 2) * 0.98 <= got <= beam * 1.06


def test_the_join_carries_the_in_plane_moment_and_the_twist(cantilever):
    ws, mixed, solid = cantilever
    for k, force in (("y", (0.0, 50.0, 0.0)), ("x", (500.0, 0.0, 0.0))):
        got = _static(ws, f"mixed_{k}", mixed, _back, _tip, force, _tip)
        ref = _static(ws, f"solid_{k}", solid, _back, _tip, force, _tip)
        d = 1 if k == "y" else 0
        assert got[d] == pytest.approx(ref[d], rel=0.03)


def test_the_mixed_cantilever_has_six_rigid_modes_and_the_solid_s_elastic_ones(cantilever):
    ws, mixed, solid = cantilever
    fm, fs = _modes(ws, "mixed_m", mixed), _modes(ws, "solid_m", solid)
    assert max(fm[:6]) < 1e-3 * fm[6]                  # no mechanism in the join
    for a, b in zip(fm[6:], fs[6:], strict=True):
        assert a == pytest.approx(b, rel=0.05)


def test_a_plate_with_a_thick_boss_matches_the_solid(tmp_path):
    mixed = _mixed(_plate_boss(), tmp_path)
    solid = _solid(_plate_boss(), tmp_path / "solid")
    m = mixed[3]
    assert m.counts["links"] == 1 and m.links[0].closed
    assert m.shell.n_elements + len(m.solid.tets) < 0.5 * len(solid[3].tets)
    fm, fs = _modes(tmp_path, "mixed_m", mixed), _modes(tmp_path, "solid_m", solid)
    assert max(fm[:6]) < 1e-3 * fm[6]
    for a, b in zip(fm[6:], fs[6:], strict=True):
        assert a == pytest.approx(b, rel=0.05)

    def edge(xyz):
        return np.abs(xyz[:, 0]) < 1e-6

    def top(xyz):
        return (np.abs(xyz[:, 2] - 23.0) < 1e-6) & (np.hypot(xyz[:, 0] - 80, xyz[:, 1] - 40) < 10 + 1e-6)

    got = _static(tmp_path, "mixed_s", mixed, edge, top, (300.0, 0.0, -600.0), top)
    ref = _static(tmp_path, "solid_s", solid, edge, top, (300.0, 0.0, -600.0), top)
    assert got[2] == pytest.approx(ref[2], rel=0.05)
    assert got[0] == pytest.approx(ref[0], rel=0.05)
