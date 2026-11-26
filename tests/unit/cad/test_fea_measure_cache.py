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


# THE SECOND DEFECT THIS PINS (26 Nov, feabench_lab/BENCH.md): the same part staged twice - by the plan step, then by
# the run - is written with a new HEADER time and a new writer count in its product name, so the key changed every
# staging and the plan step's measurement was never read back by the run (the suspected-defect checks and the
# measurement again: 33 + 4 s of the knuckle's measure + plan on a laptop, every run).
_STEP = (b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('Open CASCADE Model'),'2;1');\n"
         b"FILE_NAME('Open CASCADE Shape Model','%s',('Author'),(\n  'Open CASCADE STEP processor 7.8','',''));\n"
         b"ENDSEC;\nDATA;\n#7 = PRODUCT('Open CASCADE STEP translator 7.8 %d',\n"
         b"  'Open CASCADE STEP translator 7.8 %d','',(#8));\n#9 = CARTESIAN_POINT('',(%s));\nENDSEC;\n"
         b"END-ISO-10303-21;\n")


def _step(path: Path, when: bytes, count: int, point: bytes = b"0.,0.,0.") -> Path:
    path.write_bytes(_STEP % (when, count, count, point))
    return path


def test_the_same_solid_written_twice_is_one_key(tmp_path):
    a = _step(tmp_path / "a.step", b"2026-11-26T07:01:49", 1)
    b = _step(tmp_path / "b.step", b"2026-11-26T07:01:51", 2)
    assert a.read_bytes() != b.read_bytes()
    assert MC.key_for(a, mm_per_unit=1000.0) == MC.key_for(b, mm_per_unit=1000.0)
    # ...but another solid is another key, whatever its header
    c = _step(tmp_path / "c.step", b"2026-11-26T07:01:49", 1, b"0.,0.,1.")
    assert MC.key_for(c, mm_per_unit=1000.0) != MC.key_for(a, mm_per_unit=1000.0)
    # a file that is not a STEP is keyed by its bytes, as before
    other = tmp_path / "geometry.brep"
    other.write_bytes(b"DBRep_DrawableShape")
    assert MC.solid_digest(other) == MC.file_digest(other)


def test_two_real_stagings_of_one_part_share_the_key(tmp_path):
    import pytest
    gmsh = pytest.importorskip("gmsh")
    paths = []
    for k in range(2):
        gmsh.initialize()
        try:
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.model.occ.addBox(0, 0, 0, 0.02, 0.01, 0.005)
            gmsh.model.occ.synchronize()
            p = tmp_path / f"s{k}.step"
            for _ in range(k + 1):                # the writer's count moves on with each write in a session
                gmsh.write(str(p))
            paths.append(p)
        finally:
            gmsh.finalize()
    assert MC.key_for(paths[0], mm_per_unit=1000.0) == MC.key_for(paths[1], mm_per_unit=1000.0)
