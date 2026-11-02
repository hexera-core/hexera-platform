# Responsibility: The mixed shell + solid accuracy gate on five generated part families (an L bracket with a thick
# pad, a box with thick corner columns, a stiffened panel with a thick mount, a tube with a thick flange, a casting
# with ribs running into a block): the mixed mesh against the FEA path's own all-solid mesh of the same part, for
# a static load through the thick region (displacement at the load and at a thin-wall point, von Mises on a wall
# away from every junction) and free-free modes 7 to 12, with the element counts. (The cantilever in a block and
# the plate with a boss are in test_mixed_textbook.py.)
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
    return lambda p: (p[:, 0] >= x0) & (p[:, 0] <= x1) & (p[:, 1] >= y0) & (p[:, 1] <= y1) & (p[:, 2] >= z0) & (p[:, 2] <= z1)


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
                           "probes": {"column top": top, "wall middle": _bb(30, 40, -1, t + 1, 15, 25)},
                           "stress": ([35.0, 0.0, 30.0], [0, -1, 0], t)}
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
    if name == "casting_ribs_block":
        shape = fuse(_box(0, 0, 0, 120, 80, t), *[_box(0, y - t / 2, t, 90, t, 20) for y in (16, 40, 64)],
                     _box(90, 0, t, 30, 80, 20))
        top = _bb(89.9, 120.1, -1, 81, t + 19.99, t + 20.01)
        return shape, {"fix": _bb(-.01, .01, -1, 81, -1, 40), "load": top, "force": (0.0, 300.0, -1500.0),
                           "probes": {"block top": top, "plate": _bb(40, 50, 23, 33, -1, t + 1)},
                           "stress": ([35.0, 28.0, 0.0], [0, 0, -1], t)}
    raise KeyError(name)


def _static(ws: Path, job: str, model, spec) -> tuple[dict, float]:
    deck, xyz, shell_nodes, _m = model
    fix, load = np.flatnonzero(spec["fix"](xyz)) + 1, np.flatnonzero(spec["load"](xyz)) + 1
    probes = {k: np.flatnonzero(f(xyz)) + 1 for k, f in spec["probes"].items()}
    assert len(fix) and len(load) and all(len(v) for v in probes.values())
    lines = tb._ids("FIX", fix) + tb._ids("LOAD", load)
    for i, ids in enumerate(probes.values()):
        lines += tb._ids(f"P{i}", ids)
    f = spec["force"]
    lines += (["*BOUNDARY", "FIX, 1, 3"] + [f"{i}, 4, 6" for i in fix.tolist() if i in shell_nodes]
              + ["*STEP", "*STATIC", "*CLOAD"]
              + [f"{i}, {d + 1}, {f[d] / len(load):.9g}" for i in load.tolist() for d in range(3) if f[d]]
              + [f"*NODE PRINT, NSET=P{i}\nU" for i in range(len(probes))]
              + ["*NODE FILE, OUTPUT=3D", "U", "*EL FILE, OUTPUT=3D", "S", "*END STEP"])
    dat = tb._run(ws, job, deck + "\n".join(lines) + "\n")
    out: dict = {}
    for i, (k, _ids) in enumerate(probes.items()):
        block = dat.lower().split(f"displacements (vx,vy,vz) for set p{i} ", 1)[1].split("displacements", 1)[0]
        rows = [ln.split() for ln in block.splitlines()[1:]]
        out[k] = np.array([[float(v) for v in r[1:4]] for r in rows if len(r) == 4 and r[0].isdigit()]).mean(axis=0)
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
    # the stress AT the point: a plane through each component's nodal values on the face round it (the nearest
    # few elements of either mesh), read there - the nearest node of a 5 mm shell mesh sat 2.5 mm off, where the
    # bending moment of a loaded leg is 5% different
    near = np.argsort(d)[: max(12, int(np.sum(d <= 2.5 * np.sort(d)[min(6, len(d) - 1)])))]
    e1 = np.cross(nrm, [1.0, 0, 0] if abs(nrm[0]) < 0.9 else [0, 1.0, 0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(nrm, e1)
    a = np.column_stack([np.ones(len(near)), r[near] @ e1, r[near] @ e2])
    s = np.array([st[int(ids[ok[k]])] for k in near.tolist()])
    sx, sy, sz, sxy, syz, szx = np.linalg.lstsq(a, s, rcond=None)[0][0]
    return float(np.sqrt(0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2) + 3 * (sxy ** 2 + syz ** 2 + szx ** 2)))


@pytest.mark.parametrize("name", ["l_bracket_pad", "box_corners", "stiff_panel_mount", "tube_flange",
                                  "casting_ribs_block"])
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
    assert max(fm[:6]) < 1e-3 * fm[6], fm[:7]          # no mechanism in the joins
    assert n_mixed < n_solid
    for k, e in errs:
        assert abs(e) <= TOL, (k, e)
    assert vm == pytest.approx(vm_ref, rel=STRESS)
    for a, b in zip(fm[6:], fs[6:], strict=True):
        assert a == pytest.approx(b, rel=TOL)
