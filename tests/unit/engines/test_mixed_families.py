# Responsibility: The mixed shell + solid accuracy gate on five generated part families (an L bracket with a thick
# pad, a box with thick corner columns, a stiffened panel with a thick mount, a tube with a thick flange, a casting
# with ribs running into a block): the mixed mesh against the FEA path's own all-solid mesh of the same part, for
# a static load through the thick region (displacement at the load and at a thin-wall point, von Mises on a wall
# away from every junction) and free-free modes 7 to 12, with the element counts. (The cantilever in a block and
# the plate with a boss are in test_mixed_textbook.py.) The box, the stiffened panel and the casting have walls too
# thick for shells (MIXED-8: they reach 3.5 to 8.3 walls from their joints) and are meshed solid, which
# test_mixed_mesh.py holds; they stay here as shapes, and a plate with a lip too thick for shells (kept solid, the
# rest mixed) is solved in their place.
# Boundaries: needs CalculiX and MIXED_ACCURACY_FAMILIES=1 (CI's "Mixed accuracy" job; about half an hour). mm, N, MPa.
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
from tests.unit.engines import test_mixed_textbook as tb

pytestmark = pytest.mark.skipif(os.environ.get("MIXED_ACCURACY_FAMILIES", "") != "1" or not (
    tb.REQUIRE_CCX or (tb.CCX and Path(tb.CCX).exists())), reason="set MIXED_ACCURACY_FAMILIES=1 and HEXERA_CCX")
#: displacement and modes within this of the all-solid mesh; stress on a wall away from the junctions within STRESS
TOL, STRESS = 0.05, 0.10


def _bb(x0, x1, y0, y1, z0, z1):
    def inside(p):
        return (p[:, 0] >= x0) & (p[:, 0] <= x1) & (p[:, 1] >= y0) & (p[:, 1] <= y1) & (p[:, 2] >= z0) & (p[:, 2] <= z1)

    inside.centre = np.array([0.5 * (x0 + x1), 0.5 * (y0 + y1), 0.5 * (z0 + z1)])
    return inside


def _fit_at(pts: np.ndarray, vals: np.ndarray, at: np.ndarray, weights: np.ndarray | None = None) -> np.ndarray:
    """The field AT `at`: a least-squares linear fit of the nodal values over the directions the points span (a line
    of nodes is fitted along the line only), read at `at`. A plain mean of the nodes in a probe is the field where
    the NODES are, which a shell mesh and a tetrahedral mesh place differently: on the flanged tube's middle box, where
    the tube's deflection rises 25% along the box, the mixed mesh's four nodes sat 3 mm above the middle on average
    (+5.5% read) while its deflection there was the solid's within 0.4%."""
    q = pts - at
    if len(q) < 2:
        return vals.mean(axis=0)
    _u, sv, vt = np.linalg.svd(q - q.mean(axis=0), full_matrices=False)
    dirs = [vt[k] for k in range(len(sv)) if sv[0] > 0 and sv[k] >= 0.2 * sv[0]][: max(0, len(q) - 1)]
    a = np.column_stack([np.ones(len(q))] + [q @ v for v in dirs])
    w = np.ones(len(q)) if weights is None else weights
    return np.linalg.lstsq(a * w[:, None], vals * w[:, None], rcond=None)[0][0]


def _family(name: str):
    from tests.fea_fixtures import _box, _cut, _cyl, _fuse

    def fuse(*s):
        out = s[0]
        for o in s[1:]:
            out = _fuse(out, o)
        return out

    t = 3.0
    if name == "l_bracket_pad":
        shape = fuse(_box(0, 0, 0, 100, 60, t), _box(0, 0, 0, t, 60, 80), _box(70, 0, t, 30, 60, 12))
        top = _bb(69.9, 100.1, -1, 61, 14.99, 15.01)
        return shape, {"fix": _bb(-1, t + 1, -1, 61, 79.99, 80.01), "load": top, "force": (0.0, 200.0, -500.0),
                           "probes": {"pad top": top, "leg middle": _bb(35, 45, 25, 35, -1, t + 1)},
                           "stress": ([40.0, 30.0, t], [0, 0, 1], t)}
    if name == "box_corners":
        # 4 mm walls: the all-solid mesh of 3 mm ones ran a CI runner (8 GB) out of memory
        t, X, Y, Z, c = 4.0, 70.0, 60.0, 40.0, 12.0
        shape = fuse(_cut(_box(0, 0, 0, X, Y, Z), _box(t, t, t, X - 2 * t, Y - 2 * t, Z)),
                     *[_box(x, y, 0, c, c, Z) for x in (0, X - c) for y in (0, Y - c)])
        top = _bb(X - c - .01, X + .01, Y - c - .01, Y + .01, Z - .01, Z + .01)
        return shape, {"fix": _bb(-1, X + 1, -1, Y + 1, -.01, .01), "load": top, "force": (-800.0, 0.0, 0.0),
                           # the back wall, between the loaded column and its neighbour (five walls from each, four
                           # from the bottom): the front wall barely moves under this push (0.0002 mm, 0.3 MPa)
                           "probes": {"column top": top, "back wall middle": _bb(30, 40, Y - t - 1, Y + 1, 17, 27)},
                           "stress": ([35.0, Y, 22.0], [0, 1, 0], t)}
    if name == "stiff_panel_mount":
        shape = fuse(_box(0, 0, 0, 160, 100, t), _box(0, 25 - t / 2, t, 160, t, 25), _box(0, 75 - t / 2, t, 160, t, 25),
                     _box(60, 40, t, 40, 20, 20))
        top = _bb(59.9, 100.1, 39.9, 60.1, t + 19.99, t + 20.01)
        return shape, {"fix": _bb(-.01, .01, -1, 101, -1, 40), "load": top, "force": (400.0, 0.0, -1500.0),
                           "probes": {"mount top": top, "plate": _bb(25, 35, 45, 55, -1, t + 1)},
                           "stress": ([40.0, 50.0, 0.0], [0, 0, -1], t)}
    if name == "tube_flange":
        ro, fl, rf, length = 25.0, 9.0, 40.0, 120.0
        shape = _fuse(_cut(_cyl(0, 0, 0, rf, fl), _cyl(0, 0, -.5, ro - t, fl + 1)),
                      _cut(_cyl(0, 0, fl, ro, length), _cyl(0, 0, fl - 1, ro - t, length + 2)))
        end = _bb(-ro - 1, ro + 1, -ro - 1, ro + 1, fl + length - .01, fl + length + .01)
        return shape, {"fix": _bb(-rf - 1, rf + 1, -rf - 1, rf + 1, -.01, .01), "load": end, "force": (600.0, 0.0, 0.0),
                           "probes": {"tube end": end, "tube middle": _bb(-ro - 1, -ro + t + 1, -5, 5, fl + 55, fl + 65)},
                           "stress": ([-ro, 0.0, fl + 50.0], [-1, 0, 0], t)}
    if name == "plate_lip_block":
        # MIXED-8: the 12 mm lip of the 2 mm plate is too thick for shells (6 walls from its corner): kept solid with
        # the block, the plate shelled; clamped at x = 0, pushed through the block
        shape = fuse(_box(0, 0, 0, 150, 100, 2), _box(65, 40, 2, 20, 20, 15), _box(148, 0, 2, 2, 100, 12))
        top = _bb(64.9, 85.1, 39.9, 60.1, 16.99, 17.01)
        return shape, {"fix": _bb(-.01, .01, -1, 101, -1, 3), "load": top, "force": (0.0, 100.0, -200.0),
                           "probes": {"block top": top, "plate": _bb(105, 115, 45, 55, -1, 3),
                                      "lip top": _bb(147.9, 150.1, 45, 55, 13.99, 14.01)},
                           "stress": ([35.0, 50.0, 0.0], [0, 0, -1], 2.0)}
    if name == "casting_ribs_block":
        shape = fuse(_box(0, 0, 0, 120, 80, t), *[_box(0, y - t / 2, t, 90, t, 20) for y in (16, 40, 64)],
                     _box(90, 0, t, 30, 80, 20))
        top = _bb(89.9, 120.1, -1, 81, t + 19.99, t + 20.01)
        return shape, {"fix": _bb(-.01, .01, -1, 81, -1, 40), "load": top, "force": (0.0, 300.0, -1500.0),
                           "probes": {"block top": top, "plate": _bb(40, 50, 23, 33, -1, t + 1)},
                           "stress": ([35.0, 28.0, 0.0], [0, 0, -1], t)}
    raise KeyError(name)


def _shares(pts: np.ndarray, centre: np.ndarray | None) -> np.ndarray:
    """Each load node's share of the force: equal shares, corrected as little as can be so the force acts at the
    loaded face's middle (`centre`), over the directions the nodes span. Equal shares put it where the NODES are,
    which each mesh grades differently: the casting's all-solid mesh, fine by its ribs, put the block's load 8.7 mm
    nearer the clamp than the block's middle, and read 11% stiff of a uniform mesh."""
    n = len(pts)
    w0 = np.full(n, 1.0 / n)
    if centre is None or n < 3:
        return w0
    q = pts - centre
    _u, sv, vt = np.linalg.svd(q - q.mean(axis=0), full_matrices=False)
    dirs = [vt[k] for k in range(len(sv)) if sv[0] > 0 and sv[k] >= 0.2 * sv[0]][: n - 1]
    c = np.vstack([np.ones(n)] + [q @ v for v in dirs])
    want = np.zeros(len(c))
    want[0] = 1.0
    return w0 + c.T @ np.linalg.solve(c @ c.T, want - c @ w0)


def _static(ws: Path, job: str, model, spec) -> tuple[dict, float]:
    deck, xyz, shell_nodes, _m = model
    fix, load = np.flatnonzero(spec["fix"](xyz)) + 1, np.flatnonzero(spec["load"](xyz)) + 1
    probes = {k: np.flatnonzero(f(xyz)) + 1 for k, f in spec["probes"].items()}
    assert len(fix) and len(load) and all(len(v) for v in probes.values())
    lines = tb._ids("FIX", fix) + tb._ids("LOAD", load)
    for i, ids in enumerate(probes.values()):
        lines += tb._ids(f"P{i}", ids)
    f = spec["force"]
    share = _shares(xyz[load - 1], getattr(spec["load"], "centre", None))
    lines += (["*BOUNDARY", "FIX, 1, 3"] + [f"{i}, 4, 6" for i in fix.tolist() if i in shell_nodes]
              + ["*STEP", "*STATIC", "*CLOAD"]
              + [f"{i}, {d + 1}, {f[d] * w:.9g}" for i, w in zip(load.tolist(), share.tolist(), strict=True)
                 for d in range(3) if f[d]]
              + [f"*NODE PRINT, NSET=P{i}\nU" for i in range(len(probes))]
              + ["*NODE FILE, OUTPUT=3D", "U", "*EL FILE, OUTPUT=3D", "S", "*END STEP"])
    dat = tb._run(ws, job, deck + "\n".join(lines) + "\n")
    out: dict = {}
    for i, (k, _ids) in enumerate(probes.items()):
        block = dat.lower().split(f"displacements (vx,vy,vz) for set p{i} ", 1)[1].split("displacements", 1)[0]
        rows = [r for r in (ln.split() for ln in block.splitlines()[1:]) if len(r) == 4 and r[0].isdigit()]
        u = np.array([[float(v) for v in r[1:4]] for r in rows])
        at = getattr(spec["probes"][k], "centre", None)
        out[k] = u.mean(axis=0) if at is None else _fit_at(xyz[[int(r[0]) - 1 for r in rows]], u, at)
    return out, _vm_at((ws / f"{job}.frd").read_text(), *spec["stress"])


def _vm_at(frd: str, at, normal, wall: float) -> float:
    """Von Mises at the result node (a shell's expanded nodes too) nearest `at` on the face whose outward normal is
    `normal`."""
    def block(name: str, k: int) -> dict[int, list[float]]:
        out = {}
        for ln in frd.split(name, 1)[1].splitlines()[1:]:
            if ln.startswith(" -3"):
                break
            if ln.startswith(" -1"):
                out[int(ln[3:13])] = [float(ln[13 + 12 * j:25 + 12 * j]) for j in range(k)]
        return out
    nodes, st = block("    2C", 3), block("STRESS", 6)
    ids = np.array([i for i in st if i in nodes])
    x = np.array([nodes[i] for i in ids.tolist()])
    at, nrm = np.asarray(at, float), np.asarray(normal, float)
    off = (x - at) @ nrm
    ok = np.flatnonzero(np.abs(off) <= 0.05 * wall)
    r = (x[ok] - at) - np.outer(off[ok], nrm)
    d = np.linalg.norm(r, axis=1)
    # the stress AT the point: a linear fit through each component's nodal values on the face round it, read there
    # (the nearest node of a 5 mm shell mesh sat 2.5 mm off, where the bending moment of a loaded leg is 5%
    # different). The face round it is the nodes within three of the mesh's own node spacings there, weighted to
    # the nearest: a fixed count of nodes reached 44 mm along a curved shell wall - the band keeps one line of
    # nodes on a 15-degree faceted tube - into the flange's junction stress, and read the flanged tube 16% low
    first = np.argsort(d)[:12]
    p = r[first]
    spacing = float(np.median(np.sort(np.linalg.norm(p[:, None, :] - p[None, :, :], axis=2), axis=1)[:, 1]))
    near = np.flatnonzero(d <= 3.0 * spacing)
    if len(near) < 4:
        near = np.argsort(d)[:4]
    s = np.array([st[int(ids[ok[k]])] for k in near.tolist()])
    w = np.exp(-(d[near] / (1.5 * spacing)) ** 2)
    sx, sy, sz, sxy, syz, szx = _fit_at(r[near], s, np.zeros(3), w)
    return float(np.sqrt(0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2) + 3 * (sxy ** 2 + syz ** 2 + szx ** 2)))


#: Closed 2 Nov by MIXED-8, no longer mixed: the box (back wall +23%, mode 11 -5.4%: its 4 mm walls reach 5 to 6.7
#: walls from their joints, and a shell wall bends from the bottom's mid-plane, 2 mm below the solid wall's inner
#: corner) and the casting (mode 12 -6.2%: 3 mm ribs reaching 6.7 walls) - the shell idealisation, not the mesh or the
#: join (a shell mesh twice as fine moved them 0.7%, a cut 3 walls in 0.0%). The stiffened panel (7.8 to 8.3 walls)
#: goes solid with them. test_mixed_mesh.py holds all three to the all-solid route.
@pytest.mark.parametrize("name", ["l_bracket_pad", "tube_flange", "plate_lip_block"])
def test_a_mixed_family_matches_its_all_solid_mesh(name, tmp_path):
    shape, spec = _family(name)
    mixed = tb._mixed(shape, tmp_path)
    solid = tb._solid(shape, tmp_path / "solid")
    n_mixed, n_solid = tb._n(mixed), tb._n(solid)
    tb._record(f"mixed_{name}", "size", "elements, mixed over all-solid", n_mixed, n_solid, 1.0, n_mixed,
               solid_elements=n_solid)
    got, vm = _static(tmp_path, "mixed_s", mixed, spec)
    ref, vm_ref = _static(tmp_path, "solid_s", solid, spec)
    errs = []
    for k in spec["probes"]:
        d = int(np.argmax(np.abs(ref[k])))
        tb._record(f"mixed_{name}", "static", f"{k} displacement vs all-solid", got[k][d], ref[k][d], TOL, n_mixed)
        errs.append((k, got[k][d] / ref[k][d] - 1.0))
    tb._record(f"mixed_{name}", "static", "von Mises on a wall away from the junctions vs all-solid", vm, vm_ref,
               STRESS, n_mixed)
    fm, fs = tb._modes(tmp_path, "mixed_m", mixed), tb._modes(tmp_path, "solid_m", solid)
    for k, (a, b) in enumerate(zip(fm[6:], fs[6:], strict=True), start=7):
        tb._record(f"mixed_{name}", "modal", f"mode {k} vs all-solid", a, b, TOL, n_mixed)
    if tb.TRUTH_WALLS:                                  # the lab's converged reference, both models against it
        truth = tb._truth(shape, tmp_path, spec["stress"][2] / tb.TRUTH_WALLS)
        n_truth = tb._n(truth)
        tru, vm_tru = _static(tmp_path, "truth_s", truth, spec)
        ft = tb._modes(tmp_path, "truth_m", truth)
        for label, g, v, f in (("mixed", got, vm, fm), ("all-solid", ref, vm_ref, fs)):
            for k in spec["probes"]:
                d = int(np.argmax(np.abs(tru[k])))
                tb._record(f"mixed_{name}", "static", f"{k} displacement, {label} vs converged truth", g[k][d],
                           tru[k][d], TOL, n_mixed, truth_elements=n_truth)
            tb._record(f"mixed_{name}", "static", f"von Mises on the wall, {label} vs converged truth", v, vm_tru,
                       TOL, n_mixed, truth_elements=n_truth)
            for k, (a, b) in enumerate(zip(f[6:], ft[6:], strict=True), start=7):
                tb._record(f"mixed_{name}", "modal", f"mode {k}, {label} vs converged truth", a, b, TOL, n_mixed,
                           truth_elements=n_truth)
    assert max(fm[:6]) < 1e-3 * fm[6], fm[:7]          # no mechanism in the joins
    assert n_mixed < n_solid
    for k, e in errs:
        assert abs(e) <= TOL, (k, e)
    assert vm == pytest.approx(vm_ref, rel=STRESS)
    for a, b in zip(fm[6:], fs[6:], strict=True):
        assert a == pytest.approx(b, rel=TOL)
