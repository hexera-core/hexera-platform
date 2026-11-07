# Responsibility: Verify the parts model the checklist turns in 3D (render/part_model): every triangle keeps the part
# (or area) it belongs to through the simplification, a big model is held to the budget while every small part keeps
# its shape, corners stay crisp and curves smooth, each separate piece of an area gets a ring position, and the
# payload decodes to what the console draws; the checklist stores it beside the pictures and copies it with them.
# Boundaries: numpy only (no pyvista, no OpenCASCADE); the store is a dict.
from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import numpy as np

from meshpipeline.render import part_model as PMod


def _box(x0: float, size: float, n: int):
    """A closed box at x0, each side split into n x n squares: (points, triangles)."""
    pts, tris = [], []
    for axis in range(3):
        for side in (0.0, 1.0):
            base = len(pts)
            for i in range(n + 1):
                for j in range(n + 1):
                    p = [0.0, 0.0, 0.0]
                    p[axis] = side
                    p[(axis + 1) % 3], p[(axis + 2) % 3] = i / n, j / n
                    pts.append([x0 + p[0] * size, p[1] * size, p[2] * size])
            for i in range(n):
                for j in range(n):
                    a = base + i * (n + 1) + j
                    tris += [[a, a + n + 1, a + 1], [a + 1, a + n + 1, a + n + 2]]
    return np.array(pts), np.array(tris)


def _surface(parts):
    """`parts`: [(name prefix, x0, size, n)] -> a labelled surface, one face label per box side."""
    xyz, tris, label, names = [], [], [], []
    for prefix, x0, size, n in parts:
        p, t = _box(x0, size, n)
        per_side = len(t) // 6
        for side in range(6):
            names.append(f"{prefix}_Face_{len(names)}")
        label += [len(names) - 6 + k // per_side for k in range(len(t))]
        tris.append(t + sum(len(q) for q in xyz)); xyz.append(p)
    return SimpleNamespace(xyz=np.vstack(xyz), tris=np.vstack(tris), label=np.array(label), names=names)


def _decode(m):
    u = lambda k, dt: np.frombuffer(base64.b64decode(m[k]), dtype=dt)  # noqa: E731
    return (u("points_b64", np.float32).reshape(-1, 3), u("tris_b64", np.uint32).reshape(-1, 3),
            u("part_b64", np.uint16), u("normals_b64", np.float32).reshape(-1, 3))


def test_every_triangle_keeps_its_part_and_the_payload_decodes():
    s = _surface([("Part_1", 0.0, 10.0, 4), ("Part_2", 20.0, 2.0, 2)])
    m = PMod.model_payload(s, ["Part_1", "Part_2"])
    pts, tris, part, nrm = _decode(m)
    assert m["order"] == ["Part_1", "Part_2"] and len(tris) == m["triangles"] == len(s.tris)
    assert np.bincount(part).tolist() == [0, 6 * 32, 6 * 8]          # slot 0 (the grey rest) is empty here
    assert tris.max() < len(pts) and len(nrm) == len(pts)
    # a box keeps its corners: every point's normal is a face's own (axis-aligned)
    assert np.allclose(np.sort(np.abs(nrm), axis=1)[:, :2], 0, atol=1e-5)
    assert len(np.frombuffer(base64.b64decode(m["edges_b64"]), dtype=np.uint32)) > 0
    json.dumps(m)                                                     # stored as JSON


def test_a_big_model_is_held_to_the_budget_and_a_small_part_keeps_its_shape():
    s = _surface([("Part_1", 0.0, 100.0, 40), ("Part_2", 200.0, 1.0, 3)])      # 19,200 + 108 triangles
    m = PMod.model_payload(s, ["Part_1", "Part_2"], max_triangles=2_000)
    _pts, tris, part, _n = _decode(m)
    assert len(tris) <= 2_000 and m["simplified_from"] == len(s.tris)
    assert np.bincount(part, minlength=3)[2] == 108                    # the bolt drawn whole
    assert np.bincount(part, minlength=3)[1] >= 0.5 * (2_000 - 108)   # the plate takes the rest of the budget


def test_an_area_simplified_with_its_part_is_never_lost():
    s = _surface([("Part_1", 0.0, 100.0, 40), ("Feature_1", 50.0, 0.05, 1)])        # a speck on a big plate
    m = PMod.model_payload(s, ["Part_1", "Feature_1"], inside={"Feature_1": "Part_1"}, max_triangles=1_000)
    _pts, tris, part, _n = _decode(m)
    assert len(tris) <= 1_000 + PMod.PART_FLOOR and np.bincount(part, minlength=3)[2] == 12


def test_areas_inside_a_part_get_a_ring_position_per_piece():
    s = _surface([("Part_1", 0.0, 10.0, 2), ("Feature_1", 30.0, 1.0, 1), ("Feature_1", 40.0, 1.0, 1)])
    m = PMod.model_payload(s, ["Part_1", "Feature_1"])
    spots = [x for x in m["spots"] if x["part"] == "Feature_1"]
    assert len(spots) == 2 and sorted(round(x["c"][0]) for x in spots) == [30, 40]
    assert all(0.8 < x["r"] < 1.0 for x in spots)                     # half the box's diagonal


def test_the_checklist_stores_the_model_and_copies_it(monkeypatch, tmp_path):
    from meshpipeline.application import geometry_check as gc
    from meshpipeline.application import part_matches as PM
    from meshpipeline.contracts import object_storage

    objects: dict[str, bytes] = {}

    class _Store:
        def get_bytes(self, *, object_key):
            if object_key not in objects:
                raise object_storage.ObjectNotFound(object_key)
            return objects[object_key]

        def upload_file(self, *, local_path, object_key, **_):
            objects[object_key] = open(local_path, "rb").read()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: _Store())
    s = _surface([("Part_1", 0.0, 10.0, 2), ("Part_2", 20.0, 2.0, 1)])
    path = PM._model_file(s, ["Part_1", "Part_2"], tmp_path)
    key = gc.check_object_key("s1", "parts/model.json")
    objects[key] = path.read_bytes()
    assert json.loads(PM.model_bytes("s1"))["kind"] == "parts_model" and PM.model_bytes("other") is None
    assert key in PM.picture_keys("s1")                               # the privacy sweep deletes it
    copied = PM.copy_parts({"table": [{"id": "Part_1"}], "pictures": [], "model": key}, "s2")
    assert copied["model"] == gc.check_object_key("s2", "parts/model.json") and PM.model_bytes("s2") == objects[key]
    assert PM._model_file(SimpleNamespace(xyz=np.zeros((0, 3)), tris=np.zeros((0, 3)), label=[], names=[]),
                          [], tmp_path) is None                       # nothing to draw: the pictures alone
