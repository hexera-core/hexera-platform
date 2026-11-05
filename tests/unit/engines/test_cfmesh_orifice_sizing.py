# Responsibility: Verify cfMesh sizes an internal mesh from a SHORT narrowing too: the staged-wall field it sizes from
# reads an orifice plate's bore (its sharp rims read along each face's own normal), where the plain field read the pipe.
# venturi_orifice_007's 26 mm orifice read as its 53 mm pipe at every point and was sized for the pipe (2026-10-18).
from __future__ import annotations

import numpy as np
import pytest

R_PIPE, R_BORE = 0.053, 0.026


def _revolve(profile, n=48):
    th = np.linspace(0, 2 * np.pi, n, endpoint=False)
    pts = np.array([[x, rad * np.cos(t), rad * np.sin(t)] for x, rad in profile for t in th])
    tri = []
    for i in range(len(profile) - 1):
        for j in range(n):
            a, b = i * n + j, i * n + (j + 1) % n
            c, d = (i + 1) * n + (j + 1) % n, (i + 1) * n + j
            tri += [[a, b, c], [a, c, d]]
    return pts, np.asarray(tri)


def _save(path, pts, tri):
    import pyvista as pv
    pv.PolyData(pts, np.hstack([np.full((len(tri), 1), 3), tri]).ravel()).save(str(path))
    return path


def _staged(tmp_path):
    import pyvista as pv
    # pipe - plate face - 3 mm bore - plate face - pipe: a rim-only bore, as CAD tessellates it
    prof = [(0.0, R_PIPE), (0.1, R_PIPE), (0.195, R_PIPE), (0.195, R_BORE), (0.198, R_BORE),
            (0.198, R_PIPE), (0.3, R_PIPE), (0.392, R_PIPE)]
    wall = _save(tmp_path / "wall.stl", *_revolve(prof))
    caps = {}
    for name, x in (("inlet", 0.0), ("outlet", 0.392)):
        disc = pv.Disc(center=(x, 0, 0), inner=0.0, outer=R_PIPE, normal=(1, 0, 0),
                       r_res=3, c_res=48).triangulate()
        disc.save(str(tmp_path / f"{name}.stl"))
        caps[name] = str(tmp_path / f"{name}.stl")
    t = {"openings": {"inlet": {"centroid": [0.0, 0.0, 0.0]},
                      "outlet": {"centroid": [0.392, 0.0, 0.0]}},
         "wall_bounds_fluid": True}
    return t, {"wall": str(wall), **caps}


def test_the_sizing_field_reads_the_orifice_only_when_sharp_edges_are_read(tmp_path):
    from meshpipeline.engines.passage import radius_stats, staged_passage_field
    t, srcs = _staged(tmp_path)
    plain = staged_passage_field(t, srcs, "wall", [], corrected=False)
    sharp = staged_passage_field(t, srcs, "wall", [], corrected=False, read_sharp_edges=True)
    assert radius_stats(plain[2])["min"] > 1.5 * R_BORE, "the plain field saw the orifice"
    assert radius_stats(sharp[2])["min"] == pytest.approx(R_BORE, rel=0.15)


def test_cfmesh_sizes_its_internal_mesh_from_the_sharp_edge_reading(tmp_path, monkeypatch):
    import meshpipeline.engines.cfmesh.cfmesh_runner as R
    import meshpipeline.engines.passage as P
    seen: list = []
    real = P.staged_passage_field

    def _spy(*a, **kw):
        seen.append(kw.get("read_sharp_edges", False))
        return real(*a, **kw)
    monkeypatch.setattr(P, "staged_passage_field", _spy)
    t, srcs = _staged(tmp_path)
    chosen, field = R._passage_sizing(t, srcs, "wall", [])
    assert seen and seen[0] is True
    assert chosen["p05"] == pytest.approx(R_BORE, rel=0.15)
    caps = P.size_caps(chosen, target=R.CFMESH_CELLS_ACROSS)
    # the narrow boxes then hold the orifice at the target, whatever the band
    boxes = P.narrow_passage_regions(field[0], field[1], cell_m=caps["wall_cell"], areas=field[2],
                                     target=R.CFMESH_CELLS_ACROSS,
                                     detect=R.CFMESH_CELLS_ACROSS - 0.5)
    for b in boxes:
        assert 2 * b["radius_m"] / b["cell_needed_m"] >= R.CFMESH_CELLS_ACROSS - 1e-6
