# Responsibility: Verify cfMesh's passage sizing caps, the passage measure, and the resolution floor, judging no real mesh.
# The HEX-6 pilot delivered a 173 mm reducer at 8.9 cells across (4.6 at the narrowest wall)
# because the wall band defaulted to L/20 of a 1.26 m part. Sizes now come from the local radius.
from __future__ import annotations

import numpy as np

import meshpipeline.engines.cfmesh.cfmesh_runner as R
import meshpipeline.engines.passage as P
from meshpipeline.engines.cfmesh import flow_gates as G
from meshpipeline.engines.gates import GateCtx
from meshpipeline.engines.snappy import flow_gates as SG


def _cylinder(radius=0.05, length=1.0, n_around=24, n_along=40):
    th = np.linspace(0, 2 * np.pi, n_around, endpoint=False)
    xs = np.linspace(0, length, n_along)
    ring = np.stack([np.zeros_like(th), radius * np.cos(th), radius * np.sin(th)], axis=1)
    pts = np.concatenate([ring + [x, 0, 0] for x in xs])
    faces = []
    for i in range(n_along - 1):
        for j in range(n_around):
            a, b = i * n_around + j, i * n_around + (j + 1) % n_around
            c, d = a + n_around, b + n_around
            faces += [[a, b, d], [a, d, c]]
    c0, c1 = len(pts), len(pts) + 1
    pts = np.concatenate([pts, [[0, 0, 0], [length, 0, 0]]])
    for j in range(n_around):
        faces.append([c0, (j + 1) % n_around, j])
        base = (n_along - 1) * n_around
        faces.append([c1, base + j, base + (j + 1) % n_around])
    return pts, np.asarray(faces, dtype=np.int64)


def test_the_measure_counts_cells_across_a_tube():
    pts, faces = _cylinder()
    m = P.measure_passage(pts, faces, np.full(len(pts), 0.05))
    assert 2.0 < m["p05"] <= m["median"] < 9.0 and m["points"] == len(pts)
    assert P.measure_passage(pts, faces, np.zeros(len(pts))) == {}


def test_the_radius_is_read_from_a_closed_tube_with_a_point_found_inside():
    pts, faces = _cylinder(radius=0.05, n_around=48, n_along=80)
    out = P.passage_of_surface(pts, faces)
    r = out["passage_radius"]
    assert 0.04 < r["median"] < 0.06, r          # half the 100 mm bore
    assert out["passage_cells_across_local"]["points"] == len(pts)


def test_size_caps_put_thirteen_across_the_narrowest_and_typical_passage():
    caps = P.size_caps({"p05": 0.05, "median": 0.10})
    assert np.isclose(caps["wall_cell"], 2 * 0.05 / 13)
    assert np.isclose(caps["max_cell"], 2 * 0.10 / 13)
    assert np.isclose(caps["refinement_thickness"], 0.055)


def _render(tmp_path, **kw):
    ws = tmp_path
    return R.render_cfmesh_case(
        ws, surface_file="geom.fms", wall_patch="wall",
        patches=[{"name": "wall", "type": "wall"}, {"name": "inlet", "type": "inlet"},
                 {"name": "outlet", "type": "outlet"}],
        body_bbox=([0, 0, 0], [1.26, 0.2, 0.2]), L=1.26, domain_min=[0, 0, 0],
        domain_max=[1.26, 0.2, 0.2], strategy={}, cell_budget=8_000_000, **kw)


def test_the_passage_radius_caps_the_wall_band_and_the_background(tmp_path):
    base = _render(tmp_path)
    assert base["passage_caps"] is None
    capped = _render(tmp_path, passage_radius={"p05": 0.05, "median": 0.087})
    assert capped["wall_cell_size"] < base["wall_cell_size"]
    assert np.isclose(capped["wall_cell_size"], round(2 * 0.05 / 13, 6))
    assert capped["max_cell_size"] <= base["max_cell_size"]
    dic = (tmp_path / "system" / "meshDict").read_text()
    assert "refinementThickness 0.055" in dic and f"cellSize {2 * 0.05 / 13:.6g}" in dic


def _ctx(quality):
    c = GateCtx(workspace=None, engine="cfmesh", domain="", intake_patches=[], engine_params={})
    c.manifest_or_load = lambda: {"quality": quality}  # type: ignore[method-assign]
    return c


def test_the_narrowest_wall_gates_the_fill():
    ok, fb = G._gate_resolution_floor(_ctx({"passage_cells_across_local": {"median": 8.9, "p05": 4.6}}))
    assert not ok and "narrowest wall" in fb and "4.6" in fb
    assert G._gate_resolution_floor(_ctx({"passage_cells_across_local": {"median": 18.7, "p05": 17.5}}))[0]
    assert G._gate_resolution_floor(_ctx({}))[0], "an older mesh without the measure is not judged"
    # snappy holds the same bar with the same measure
    assert not SG._gate_resolution_floor(_ctx({"passage_cells_across_local": {"median": 9, "p05": 5}}))[0]
    assert SG._gate_resolution_floor(_ctx({"passage_cells_across_local": {"median": 20, "p05": 13}}))[0]
