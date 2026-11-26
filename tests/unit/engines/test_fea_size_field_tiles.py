"""TIME-12: the size field is measured a tile of points at a time - a zone group only where it can still make a point
finer - with the same sizes to the bit (a customer assembly with ~1,800 hot spots spent 25 s on each count, every
point against every group).

- the tiled sizes are the sizes of every point against every group, to the bit (random points, points at the
  surface, cubes deep in a part);
- the histogram, the plan and its estimate are the same with the tiles off, on many zones and on real parts;
- a count over ~1,800 small zones stays well under the time every-point-against-every-group took.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("OCP.BRepPrimAPI")
gmsh = pytest.importorskip("gmsh")
pytest.importorskip("vtkmodules.vtkFiltersCore")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.engines import fea_driver as FD  # noqa: E402
from meshpipeline.engines import fea_plan as P  # noqa: E402
from meshpipeline.engines import fea_size_field as SF  # noqa: E402


def _cubes(n: int, side: float = 2.0, pitch: float = 5.0):
    """n x n small cubes (mm) in a grid, each its own part, outward facing, faces Part_k_Face_j."""
    quads = ((0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5))
    xyz: list = []
    tris: list = []
    label: list = []
    names: list = []
    k = 0
    for a in range(n):
        for b in range(n):
            k += 1
            x, y, z = a * pitch, b * pitch, (a * 7 + b * 3) % 5 * 0.5
            base = len(xyz)
            xyz += [(x + side * i, y + side * j, z + side * m) for i in (0, 1) for j in (0, 1) for m in (0, 1)]
            for j, (q0, q1, q2, q3) in enumerate(quads, start=1):
                names.append(f"Part_{k}_Face_{j}")
                tris += [(base + q0, base + q2, base + q1), (base + q0, base + q3, base + q2)]
                label += [len(names) - 1] * 2
    return SimpleNamespace(xyz=np.asarray(xyz, float), tris=np.asarray(tris), label=np.asarray(label), names=names)


def _zones(surf, n: int) -> list[dict]:
    """Every kind of zone on every cube, of sizes spread over many groups: holes, fillets, thin spots (balls),
    capped corners (an edge two faces share) and, on every third cube, the part's own size."""
    zones: list[dict] = []
    for k in range(1, n * n + 1):
        s = 0.1e-3 * (1.0 + 0.37 * (k % 23))
        corner = surf.xyz[(k - 1) * 8] * 1e-3
        zones.append({"kind": "hole", "faces": [f"Part_{k}_Face_1"], "size_m": s, "reach_m": 4 * s})
        zones.append({"kind": "fillet", "faces": [f"Part_{k}_Face_2"], "size_m": 1.3 * s, "reach_m": 9 * s})
        zones.append({"kind": "wall_spot", "faces": [f"Part_{k}_Face_4"], "size_m": 0.8 * s, "reach_m": 2 * s,
                      "at_m": list(corner + 1e-3), "radius_m": 0.5e-3})
        if k % 4 == 0:
            zones.append({"kind": "corner", "faces": [f"Part_{k}_Face_1", f"Part_{k}_Face_3"], "size_m": 0.7 * s,
                          "reach_m": 3 * s})
        if k % 3 == 0:
            zones.append({"kind": "part", "part": f"Part_{k}", "faces": [f"Part_{k}_Face_{j}" for j in range(1, 7)],
                          "size_m": 0.6e-3, "reach_m": 0.0})
    return zones


def _many(n: int = 8):
    surf = _cubes(n)
    return surf, _zones(surf, n)


def _same(a: SF.Histogram, b: SF.Histogram) -> None:
    assert a.rows == b.rows and a.surface == b.surface
    assert (a.complete, a.cubes, a.stopped) == (b.complete, b.cubes, b.stopped)


@pytest.mark.parametrize("r", [0.0, 0.4e-3])
def test_the_tiled_sizes_are_every_groups_sizes_to_the_bit(r):
    surf, zones = _many()
    field = SF.SizeField(surf, zones, growth=1.25)
    assert len(field.groups) > 100
    rng = np.random.default_rng(7)
    lo, hi = field.xyz.min(axis=0), field.xyz.max(axis=0)
    span = hi - lo
    near = field.xyz[rng.integers(0, len(field.xyz), 20_000)] + rng.normal(0.0, 0.3e-3, (20_000, 3))
    for p in (lo - 0.1 * span + rng.random((30_000, 3)) * 1.2 * span, near):
        tiled = field.sizes(p, r)
        each = field._sizes_each(p, r)
        assert np.array_equal(tiled[0], each[0]) and np.array_equal(tiled[1], each[1])
        assert np.isfinite(tiled[0]).all()


def test_the_histogram_is_the_same_with_the_tiles_off(monkeypatch):
    surf, zones = _many()
    kw = {"coarsest": 4e-3, "per_h3": P.PER_H3, "stop_elements": float("inf"), "workers": 1, "max_cubes": 400_000}
    tiled = SF.SizeField(surf, zones, growth=1.25).histogram(**kw)
    monkeypatch.setattr(SF, "TILE_POINTS", 0)
    each = SF.SizeField(surf, zones, growth=1.25).histogram(**kw)
    _same(tiled, each)
    assert tiled.cubes > 100_000


def _stage(shape, ws: Path) -> Path:
    """Stage a part as the builder does for gmsh: the upload read in mm, written in metres."""
    ws.mkdir(parents=True, exist_ok=True)
    src = fx.write_step(shape, ws / "upload.step")
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("Geometry.OCCScaling", 0.001)
        gmsh.model.occ.importShapes(str(src))
        gmsh.option.setNumber("Geometry.OCCScaling", 1.0)
        gmsh.model.occ.synchronize()
        gmsh.write(str(ws / "geometry.step"))
    finally:
        gmsh.finalize()
    return ws


@pytest.mark.parametrize("name", ["plate_with_hole", "bracket", "touching_blocks", "drilled_plates"])
def test_the_plan_and_its_estimate_are_the_same_with_the_tiles_off(name, tmp_path, monkeypatch):
    shape = fx.drilled_plates(4) if name == "drilled_plates" else getattr(fx, name)()
    record, surface, diag = FD.measure_staged(_stage(shape, tmp_path / name))
    monkeypatch.setattr(SF, "plan_workers", lambda: 1)       # in this process, so the switch below reaches it
    tiled = FD.make_plan(record, surface, diag)
    monkeypatch.setattr(SF, "TILE_POINTS", 0)
    each = FD.make_plan(record, surface, diag)
    assert tiled == each and tiled["estimate"] == each["estimate"]


def test_a_count_over_many_small_zones_is_quick(monkeypatch):
    """~1,900 zone groups (a customer assembly had 1,764 hot spots): one count measured each point against every
    group's box (15 s here, 25 s a count on the customer's); tiled, only the groups that reach a tile are looked at
    (5 s here). The work is checked, not only the time: the points looked at, group by group."""
    surf, zones = _many(24)
    field = SF.SizeField(surf, zones, growth=1.25)
    assert len(field.groups) > 1500
    looked = [0, 0]
    each = SF.SizeField._sizes_each

    def counted(self, p, r, h=None, need=None, groups=None):
        looked[0] += len(p) * (len(self.groups) if groups is None else len(groups))
        return each(self, p, r, h, need, groups)

    tiled = SF.SizeField._sizes_tiled

    def every(self, p, r):
        looked[1] += len(p) * len(self.groups)
        return tiled(self, p, r)

    monkeypatch.setattr(SF.SizeField, "_sizes_each", counted)
    monkeypatch.setattr(SF.SizeField, "_sizes_tiled", every)
    t = time.perf_counter()
    hist = field.histogram(coarsest=4e-3, per_h3=P.PER_H3, stop_elements=float("inf"), workers=1,
                           max_cubes=600_000)
    took = time.perf_counter() - t
    assert hist.cubes > 200_000
    assert looked[0] < 0.05 * looked[1], looked       # well under every point x every group (2%)
    assert took < 40.0, f"one count took {took:.1f} s"


def test_one_sizings_passes_share_the_processes_and_the_distances(monkeypatch):
    """Within one sizing the counting processes are started once and told the zones of each pass, and the surface
    distances are kept from the last field - with the same histograms as fields counted alone."""
    surf, zones = _many(6)
    monkeypatch.setattr(SF, "PARALLEL_MIN_POINTS", 2000)
    kw = {"coarsest": 4e-3, "per_h3": P.PER_H3, "stop_elements": float("inf"), "workers": 2, "max_cubes": 100_000}
    alone = [SF.SizeField(surf, z, growth=1.25).histogram(**kw) for z in (zones, zones[3:])]
    with SF.sizing():
        a = SF.SizeField(surf, zones, growth=1.25)
        first = a.histogram(**kw)
        pool = SF._scope().pool
        b = SF.SizeField(surf, zones[3:], growth=1.25)
        second = b.histogram(**kw)
        assert pool is not None and SF._scope().pool is pool
        assert b.solid is a.solid
        assert {id(g.faces) for g in a.groups} & {id(g.faces) for g in b.groups if g.faces is not None}
    assert SF._scope().pool is None and SF._scope().dists is None
    assert all(proc.poll() is not None for proc in pool.procs)
    _same(first, alone[0])
    _same(second, alone[1])
