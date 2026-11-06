# Responsibility: Hold the FEA measurement cache to serving a record only for the very same solid file measured
# the same way - its bytes, its unit, its ray budget, and the measuring code.
# Boundaries: a fake object store; no CAD is measured here.
from __future__ import annotations

from pathlib import Path

from meshpipeline.cad import fea_measure_cache as MC
from meshpipeline.contracts.object_storage import ObjectNotFound

# THE DEFECT THIS PINS. Shared dev, 1 Nov: an IGES landing gear's FEA plan measured its staged solid from nothing
# on every run (1090 s offline, 23+ min on a worker) although the solid had not changed.


class _Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def upload_file(self, *, local_path, object_key, **_):
        self.objects[object_key] = Path(local_path).read_bytes()

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise ObjectNotFound(object_key)
        return self.objects[object_key]


def test_the_same_file_measured_the_same_way_is_read_back(tmp_path, monkeypatch):
    from meshpipeline.contracts import object_storage

    store = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: store)
    solid = tmp_path / "geometry.step"
    solid.write_bytes(b"ISO-10303-21; the part")
    key = MC.key_for(solid, mm_per_unit=1000.0, max_ray_triangles=5_000_000)
    assert MC.load(key) is None
    MC.save(key, {"record": {"bodies": 11, "hot_spots": []}, "healed": False})
    assert MC.load(key) == {"record": {"bodies": 11, "hot_spots": []}, "healed": False}


def test_anything_the_measurement_reads_is_in_the_key(tmp_path):
    solid = tmp_path / "geometry.step"
    solid.write_bytes(b"ISO-10303-21; the part")
    key = MC.key_for(solid, mm_per_unit=1000.0, max_ray_triangles=5_000_000)
    assert MC.key_for(solid, mm_per_unit=1.0, max_ray_triangles=5_000_000) != key
    assert MC.key_for(solid, mm_per_unit=1000.0, max_ray_triangles=600_000) != key
    solid.write_bytes(b"ISO-10303-21; another part")
    assert MC.key_for(solid, mm_per_unit=1000.0, max_ray_triangles=5_000_000) != key


def test_a_store_that_fails_is_a_miss_never_an_error(tmp_path, monkeypatch):
    from meshpipeline.contracts import object_storage

    def boom():
        raise RuntimeError("no store")
    monkeypatch.setattr(object_storage, "get_object_store", boom)
    assert MC.load("fea_measure_cache/x.json") is None
    MC.save("fea_measure_cache/x.json", {"record": {}})
