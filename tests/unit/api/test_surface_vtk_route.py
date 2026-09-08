# Responsibility: Verify the ParaView export route serves the stored viewer payload as a VTK file,
# refuses a job without a polyMesh surface, and shows a foreign tenant nothing.
from __future__ import annotations

import base64
import json

import numpy as np
from tests.unit.api.test_viewer_endpoints import _PAYLOAD, JOB, _client, _Store


def _polymesh_payload() -> dict:
    pts = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=np.float32)
    polys = np.array([4, 0, 1, 2, 3], dtype=np.uint32)
    b64 = lambda a: base64.b64encode(a.tobytes()).decode("ascii")   # noqa: E731
    surface = {
        "kind": "polymesh", "mesh_units": "m", "is_mesh": True,
        "patches": [{"name": "wall", "type": "wall", "face_count": 1,
                     "points_b64": b64(pts), "polys_b64": b64(polys)}],
        "quality_fields": {"basis": "owner_cell_max",
                           "metrics": {"non_ortho": {"limit": 65.0, "max": 12.5}},
                           "patches": {"wall": {"non_ortho_b64": b64(np.array([12.5], np.float32))}},
                           "hotspots": []},
    }
    return {**_PAYLOAD, "surface": surface}


def test_the_vtk_route_serves_the_stored_surface_with_its_fields(monkeypatch):
    store = _Store({"jobs/x/viewer_data.json": json.dumps(_polymesh_payload()).encode()})
    client, store = _client(monkeypatch, store=store)

    r = client.get(f"/api/v1/simulation/{JOB}/surface.vtk")

    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/octet-stream")
    assert r.headers["content-disposition"] == f'attachment; filename="mesh_quality_{JOB[:8]}.vtk"'
    assert r.content.startswith(b"# vtk DataFile Version 3.0")
    assert b"POLYGONS 1 5\n" in r.content and b"SCALARS non_ortho float 1" in r.content
    assert store.reads == ["jobs/x/viewer_data.json"]        # the bytes came from the store


def test_an_input_surface_fallback_has_no_vtk_export(monkeypatch):
    client, _ = _client(monkeypatch)                          # the stl payload of the base fixture
    r = client.get(f"/api/v1/simulation/{JOB}/surface.vtk")
    assert r.status_code == 404
    assert "polyMesh" in r.json()["detail"]


def test_a_foreign_owner_sees_nothing(monkeypatch):
    client, store = _client(monkeypatch, owner="owner-other")
    r = client.get(f"/api/v1/simulation/{JOB}/surface.vtk")
    assert r.status_code == 404 and store.reads == []
