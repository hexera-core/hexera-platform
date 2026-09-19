# Responsibility: Verify a VMTK delivery reaches the viewer with per-face quality fields aligned
# with the triangles it draws, patch by patch, from the tets in mesh.vtu.
# Boundaries: a hand-built mesh.vtu in vmtk's shape - tets plus explicit boundary triangles
# carrying CellEntityIds (1 = wall, caps from 2).
from __future__ import annotations

import base64

import numpy as np
import pytest

pv = pytest.importorskip("pyvista")

from meshpipeline.engines.vmtk.viewer_surface import read_vtu, surface_patches  # noqa: E402

CUBE = np.asarray([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
                   (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], dtype=float)
KUHN = [(0, 1, 2, 6), (0, 2, 3, 6), (0, 3, 7, 6), (0, 7, 4, 6), (0, 4, 5, 6), (0, 5, 1, 6)]
WALL = [(0, 5, 1), (0, 4, 5), (2, 3, 6), (3, 7, 6), (0, 1, 2), (0, 2, 3), (4, 5, 6), (7, 4, 6)]
CAP_A = [(0, 3, 7), (0, 7, 4)]          # x = 0
CAP_B = [(1, 2, 6), (5, 1, 6)]          # x = 1


def _f32(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype=np.float32)


def _write_vtu(ws, *, entity_ids: bool = True) -> None:
    cells, types, ids = [], [], []
    for t in KUHN:
        cells += [4, *t]
        types.append(10)
        ids.append(0)
    for tri, eid in [(t, 1) for t in WALL] + [(t, 2) for t in CAP_A] + [(t, 3) for t in CAP_B]:
        cells += [3, *tri]
        types.append(5)
        ids.append(eid)
    grid = pv.UnstructuredGrid(np.asarray(cells), np.asarray(types, dtype=np.uint8), CUBE)
    if entity_ids:
        grid.cell_data["CellEntityIds"] = np.asarray(ids, dtype=np.int32)
    grid.save(str(ws / "mesh.vtu"))


def _hook():
    from meshpipeline.engines.vmtk.spec import _viewer_surface
    return _viewer_surface()


def test_the_wall_and_caps_are_drawn_with_their_quality_fields(tmp_path):
    _write_vtu(tmp_path)
    mesh = read_vtu(tmp_path)
    assert [b.shape[1] for b in mesh["cells"]] == [4] and len(mesh["cells"][0]) == 6
    names = [n for n, _ in mesh["patches"]]
    assert {"wall", "cap_2", "cap_3"} <= set(names)
    assert dict(mesh["patches"])["wall"].__len__() == 8
    assert len(dict(mesh["patches"])["cap_2"]) == 2 and len(dict(mesh["patches"])["cap_3"]) == 2

    resp = _hook()(tmp_path, roles={"cap_2": "inlet"}, units="m")
    assert resp["kind"] == "stl"
    qf = resp["quality_fields"]
    drawn = {p["name"]: p["tri_count"] for p in resp["patches"]}
    assert set(qf["patches"]) == set(drawn)
    for name, n in drawn.items():
        assert qf["patches"][name]["count"] == n
        for field in ("non_ortho_b64", "skewness_b64", "aspect_ratio_b64"):
            assert len(_f32(qf["patches"][name][field])) == n
    assert 0.0 < qf["metrics"]["non_ortho"]["max"] < 90.0           # tets are never orthogonal
    assert qf["metrics"]["non_ortho"]["limit"] == 65.0
    # the drawn surface is the same set of triangles surface_patches always returned
    assert {n: len(t) for n, t in surface_patches(tmp_path).items()} == drawn


def test_a_mesh_without_entity_ids_is_one_wall_with_fields(tmp_path):
    _write_vtu(tmp_path, entity_ids=False)
    resp = _hook()(tmp_path, roles={}, units="m")
    assert [p["name"] for p in resp["patches"]] == ["wall"]
    assert resp["quality_fields"]["patches"]["wall"]["count"] == resp["patches"][0]["tri_count"]


def test_no_mesh_yet_means_no_surface_and_no_fields(tmp_path):
    assert read_vtu(tmp_path) is None
    assert surface_patches(tmp_path) == {}
    assert _hook()(tmp_path, roles={}, units="m") is None
