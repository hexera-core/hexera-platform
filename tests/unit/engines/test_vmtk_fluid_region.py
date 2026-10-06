# Responsibility: Verify that VMTK fills the FLUID: the staged wall keeps the pieces the fluid sees
#                 (a bore, a centre rod) and drops the rest (a metal part's outer skin), openings are
#                 measured on their rims (a ring is as narrow as its gap) and capped the way they
#                 are shaped, and a delivered cap off its opening's area fails the mesh.
from __future__ import annotations

import json

import numpy as np
import pytest
import pyvista as pv

from meshpipeline.engines.vmtk import lumen_staging as LS
from meshpipeline.engines.vmtk import vmtk_runner as R

L = 0.4


def _tube(r, length=L, x0=0.0, inward=False):
    t = pv.Cylinder(center=(x0 + length / 2, 0, 0), direction=(1, 0, 0), radius=r, height=length,
                    resolution=64, capping=False).triangulate().subdivide(1).clean()
    return t.flip_faces() if inward else t


def _disc(r, x, r_in=0.0):
    d = pv.Disc(center=(x, 0, 0), inner=r_in, outer=r, normal=(1, 0, 0), r_res=3, c_res=64)
    return d.triangulate().clean()


def test_a_metal_pipe_keeps_its_bore_and_drops_its_outer_skin():
    skin, bore = _tube(0.05), _tube(0.04, inward=True)
    wall = skin.merge(bore)
    lids = [_disc(0.04, 0.0), _disc(0.04, L)]
    kept = LS.fluid_wall(wall, lids, (0.2, 0.01, 0.0))
    r = np.sqrt(np.asarray(kept.points)[:, 1] ** 2 + np.asarray(kept.points)[:, 2] ** 2)
    assert r.max() == pytest.approx(0.04, rel=1e-3), "the outer skin was kept"
    assert kept.n_cells == pytest.approx(bore.n_cells, rel=0.01)


def test_an_annulus_keeps_both_its_bore_and_its_rod():
    bore, rod = _tube(0.05, inward=True), _tube(0.03)
    wall = bore.merge(rod)
    lids = [_disc(0.05, 0.0, r_in=0.03), _disc(0.05, L, r_in=0.03)]
    kept = LS.fluid_wall(wall, lids, (0.2, 0.04, 0.0))
    assert kept.n_cells == wall.n_cells


def _ports(*xs, size=0.1):
    return [{"name": f"p{i}", "centroid": [x, 0.0, 0.0], "size_m": size, "area_m2": 0.0}
            for i, x in enumerate(xs)]


def test_a_ring_opening_is_measured_on_its_two_rims_and_capped_as_a_ring():
    wall = _tube(0.05, inward=True).merge(_tube(0.03))
    ports = _ports(0.0, L)
    assert LS.measure_openings(wall, ports) == "annular"
    for p in ports:
        assert p["loops"] == 2
        assert p["open_area_m2"] == pytest.approx(np.pi * (0.05 ** 2 - 0.03 ** 2), rel=0.02)
        assert p["hydraulic_m"] == pytest.approx(2 * (0.05 - 0.03), rel=0.02)   # the gap
    # the narrow end of the sizing is the gap, not the bore
    assert LS.sizing(ports)["radius_lo"] == pytest.approx(0.04 * LS.RADIUS_LO_FRACTION, rel=0.03)


def test_disk_openings_stay_simple():
    ports = _ports(0.0, L)
    assert LS.measure_openings(_tube(0.05), ports) == "simple"
    assert all(p["loops"] == 1 for p in ports)
    assert ports[0]["hydraulic_m"] == pytest.approx(0.1, rel=0.02)


def test_a_mix_of_rings_and_disks_is_refused_with_its_reason():
    # a rod that ends inside the pipe: a ring at the inlet, a disk at the outlet
    rod = pv.Cylinder(center=(0.15, 0, 0), direction=(1, 0, 0), radius=0.03, height=0.3,
                      resolution=64, capping=True).triangulate().clean()
    at_inlet = np.flatnonzero(np.abs(np.asarray(rod.cell_centers().points)[:, 0]) < 1e-9)
    rod = rod.remove_cells(at_inlet).extract_surface(algorithm="dataset_surface").clean()
    wall = _tube(0.05, inward=True).merge(rod)
    with pytest.raises(LS.OpeningShapeError, match="p0: 2 rims, p1: 1 rim"):
        LS.measure_openings(wall, _ports(0.0, L))


def test_the_remesh_keeps_every_piece_and_the_generator_caps_rings():
    surface, generate = R.build_staged_stages({"sizing_array": "LocalRadius", "wall_pieces": 2,
                                               "capping_method": "annular"})
    assert "-method all -cleanoutput 1" in " ".join(surface)
    assert "-cappingmethod annular" in " ".join(generate)
    one, gen1 = R.build_staged_stages({"sizing_array": "LocalRadius"})
    assert "-method largest" in " ".join(one) and "-cappingmethod" not in " ".join(gen1)


def _tri_patch(area, n=50):
    # a flat square of the given area as triangles (coordinate triples)
    s = np.sqrt(area)
    g = pv.Plane(i_size=s, j_size=s, i_resolution=n, j_resolution=n).triangulate()
    p = np.asarray(g.points)
    f = np.asarray(g.faces).reshape(-1, 4)[:, 1:]
    return [list(map(tuple, p[t])) for t in f]


def test_a_cap_off_its_opening_fails_the_mesh(tmp_path):
    rec = {"ports": [{"name": "inlet", "centroid": [0, 0, 0], "size_m": 0.085,
                      "area_m2": 0.0179, "open_area_m2": 0.00573},
                     {"name": "outlet", "centroid": [1, 0, 0], "size_m": 0.085,
                      "area_m2": 0.0179, "open_area_m2": 0.00573}]}
    (tmp_path / LS.STAGING_FACT).write_text(json.dumps(rec))
    patches = {"inlet": _tri_patch(0.0179), "outlet": _tri_patch(0.0058), "wall": []}
    bad = R._caps_off_their_openings(tmp_path, patches)
    assert len(bad) == 1 and "'inlet' cap covers 17,900 mm2" in bad[0]
    assert "different region than the fluid" in bad[0]
    assert R._caps_off_their_openings(tmp_path, {"inlet": _tri_patch(0.0057)}) == []


def test_a_rim_belongs_to_the_port_whose_lid_it_lies_on():
    # a 10 mm side branch whose mouth sits 50 mm from a 200 mm port's centre: by distance over
    # port size the big port is 'nearer' (0.25 vs 0.5) and the branch read as a ring
    big = _tube(0.1, length=0.3, x0=-0.3)
    small = _tube(0.005, length=0.25, x0=0.05)
    wall = big.merge(small)
    def ports():
        return [{"name": "big", "centroid": [0.0, 0, 0], "size_m": 0.2, "area_m2": 0.0},
                {"name": "branch", "centroid": [0.05, 0, 0], "size_m": 0.01, "area_m2": 0.0},
                {"name": "far_big", "centroid": [-0.3, 0, 0], "size_m": 0.2, "area_m2": 0.0},
                {"name": "far_branch", "centroid": [0.3, 0, 0], "size_m": 0.01, "area_m2": 0.0}]
    lids = [_disc(0.1, 0.0), _disc(0.005, 0.05), _disc(0.1, -0.3), _disc(0.005, 0.3)]
    on_lids = ports()
    assert LS.measure_openings(wall, on_lids, lids) == "simple"
    assert [p["loops"] for p in on_lids] == [1, 1, 1, 1]
    assert on_lids[1]["hydraulic_m"] == pytest.approx(0.01, rel=0.03)
    # by centre distance over size alone, the branch's mouth went to the big port
    with pytest.raises(LS.OpeningShapeError):
        LS.measure_openings(wall, ports())


def _radial_sign(piece_pts, piece_faces):
    # mean sign of (cell normal . radial direction from the x axis): +1 points away from the axis
    p = piece_pts[piece_faces]
    n = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    c = p.mean(axis=1)
    radial = c.copy()
    radial[:, 0] = 0.0
    return float(np.sign(np.einsum('ij,ij->i', n, radial)).mean())


def test_an_annulus_wall_is_wound_out_of_the_fluid_piece_by_piece():
    # the bore wound out of the fluid (normals away from the axis) and the rod ALSO wound away
    # from the axis - into the fluid - as vmtk's per-piece auto-orientation leaves it
    bore, rod = _tube(0.05), _tube(0.03)
    wall = bore.merge(rod).clean()
    lids = [_disc(0.05, 0.0, r_in=0.03), _disc(0.05, L, r_in=0.03)]
    out, ok = LS.orient_out_of_fluid(wall, lids, (0.2, 0.04, 0.0))
    assert ok
    pts = np.asarray(out.points)
    f = np.asarray(out.faces).reshape(-1, 4)[:, 1:]
    r = np.hypot(pts[f].mean(axis=1)[:, 1], pts[f].mean(axis=1)[:, 2])
    assert _radial_sign(pts, f[r > 0.04]) > 0.9, 'the bore must face away from the fluid'
    assert _radial_sign(pts, f[r < 0.04]) < -0.9, 'the rod must face into itself'


def test_a_layered_multi_piece_fill_runs_with_the_staged_winding_kept(tmp_path):
    s = R.resolve_strategy({'sizing_array': 'LocalRadius', 'wall_pieces': 2,
                            'wall_oriented': True, 'boundary_layers': 3})
    assert R._keeps_staged_winding(s)
    assert not R._keeps_staged_winding({**s, 'boundary_layers': 0})
    assert not R._keeps_staged_winding({**s, 'wall_pieces': 1})
    argv = R._winding_kept(tmp_path, R.build_staged_stages(s)[1])
    assert argv[1:] == ['vmtkpythonscript', '-scriptfile', 'vmtk_generate.py']
    args = json.loads((tmp_path / 'vmtk_generate_args.json').read_text())
    assert args[0] == 'vmtkmeshgenerator' and '-cappingmethod' not in args
    src = (tmp_path / 'vmtk_generate.py').read_text()
    assert 'AutoOrientNormals = 0' in src
    compile(src, 'vmtk_generate.py', 'exec')
