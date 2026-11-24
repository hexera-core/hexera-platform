# Responsibility: Hold face picking (NOTE-17, application/face_pick) to its contract: the geometry check keeps a face
# map for every upload - a single-body CAD file and a triangle file alike - and the erasure deletes it; a click on the
# skin is answered with the face's engine name, its plain label, its signature and its triangles; a click on nothing
# names nothing; no map, no answer; picked faces sent later are re-read against the map (unknown ids dropped, the
# client's own label never trusted).
# Boundaries: the store and the fetch are stood in for; parts are built in the test.
from __future__ import annotations

import base64
import json
import shutil

import numpy as np
import pytest

from meshpipeline.application import face_pick
from meshpipeline.application import geometry_check as gc
from meshpipeline.contracts import object_storage

pytest.importorskip("OCP.BRepPrimAPI")

SID = "00000000-0000-0000-0000-00000000f1ce"
SOURCE = {"source_id": "src-1", "owner_id": "owner-1", "object_key": "uploads/part", "sha256": "0" * 64,
          "size_bytes": 1, "original_filename": "part.step", "suffix_hint": ".step"}


class _Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get_bytes(self, *, object_key):
        if object_key not in self.objects:
            raise object_storage.ObjectNotFound(object_key)
        return self.objects[object_key]

    def upload_file(self, *, local_path, object_key, **_):
        from pathlib import Path
        self.objects[object_key] = Path(local_path).read_bytes()


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(object_storage, "get_object_store", lambda: s)
    face_pick._cache.clear()
    return s


def _serve(monkeypatch, path):
    def fetch(ref, work):
        dest = work / f"upload{path.suffix}"
        shutil.copyfile(path, dest)
        return dest
    monkeypatch.setattr(gc, "_fetch", fetch)


def _checked(store, monkeypatch, path, scale=0.001) -> dict:
    """The face map kept for `path` as the naming keeps it, and the check stored with its facts."""
    _serve(monkeypatch, path)
    stored = {"source": {**SOURCE, "suffix_hint": path.suffix, "original_filename": f"part{path.suffix}"}}
    facts = {"scale_to_m": scale}
    gc.keep_face_map(SID, stored, facts, None)
    store.objects[gc.check_object_key(SID, "scout.json")] = json.dumps({"status": "ready", "facts": facts}).encode()
    return facts


def _plate_step(tmp_path):
    from tests import fea_fixtures as fx
    return fx.write_step(fx.plate_with_hole(), tmp_path / "plate.step")


def test_a_single_body_cad_upload_keeps_its_face_map(store, monkeypatch, tmp_path):
    facts = _checked(store, monkeypatch, _plate_step(tmp_path))
    kept = facts[face_pick.FACT_KEY]
    assert kept["basis"] == "cad" and kept["faces"] >= 7 and kept["triangles"] > 0 and kept["token"]
    assert gc.check_object_key(SID, "faces.npz") in store.objects


def test_a_click_on_the_bore_answers_its_face_label_and_triangles(store, monkeypatch, tmp_path):
    _checked(store, monkeypatch, _plate_step(tmp_path))
    got = face_pick.face_at(SID, [0.0675, 0.030, 0.002], [1, 0, 0])
    assert got["id"].startswith("Face_") and got["label"].endswith("round Ø15 mm")
    assert got["sig"]["area_m2"] > 0 and len(got["sig"]["centroid_m"]) == 3
    pts = np.frombuffer(base64.b64decode(got["highlight"]["points_b64"]), dtype=np.float32).reshape(-1, 3)
    tris = np.frombuffer(base64.b64decode(got["highlight"]["tris_b64"]), dtype=np.uint32).reshape(-1, 3)
    assert len(tris) and int(tris.max()) < len(pts)
    # every highlighted point is on the bore: 7.5 mm from its axis
    assert np.allclose(np.hypot(pts[:, 0] - 0.060, pts[:, 1] - 0.030), 0.0075, atol=2e-4)


def test_a_click_on_nothing_names_nothing_and_no_map_answers_none(store, monkeypatch, tmp_path):
    assert face_pick.face_at(SID, [0.0, 0.0, 0.0]) is None                     # no check, no map
    _checked(store, monkeypatch, _plate_step(tmp_path))
    assert face_pick.face_at(SID, [0.06, 0.03, 0.5], [0, 0, 1]) == {"id": None}


def test_a_unit_confirmed_after_the_map_was_made_is_followed(store, monkeypatch, tmp_path):
    facts = _checked(store, monkeypatch, _plate_step(tmp_path))
    # the user confirmed inches: the skin and the facts were rescaled by 25.4, the map was not
    facts["scale_to_m"] = 0.0254
    store.objects[gc.check_object_key(SID, "scout.json")] = json.dumps({"status": "ready", "facts": facts}).encode()
    got = face_pick.face_at(SID, [0.0675 * 25.4, 0.030 * 25.4, 0.002 * 25.4], [1, 0, 0])
    assert got["label"].endswith("round Ø15 mm")         # the label says the map's own millimetres
    assert got["sig"]["centroid_m"][0] == pytest.approx(0.060 * 25.4, rel=1e-3)


def test_a_triangle_upload_keeps_its_regions(store, monkeypatch, tmp_path):
    from tests import fea_fixtures as fx

    from meshpipeline.cad.stl_io import write_stl_binary

    tris = fx.stl_triangles(fx.plate_with_hole()) / 1000.0        # written in metres, read at scale 1
    stl = tmp_path / "plate.stl"
    write_stl_binary(stl, [(t[0].tolist(), t[1].tolist(), t[2].tolist()) for t in tris])
    facts = _checked(store, monkeypatch, stl, scale=1.0)
    assert facts[face_pick.FACT_KEY]["basis"] == "triangles"
    got = face_pick.face_at(SID, [0.010, 0.010, 0.004], [0, 0, 1])
    assert got["id"].startswith("Region_") and got["label"].endswith("flat 120 × 60 mm")


def test_picked_faces_are_reread_against_the_map(store, monkeypatch, tmp_path):
    _checked(store, monkeypatch, _plate_step(tmp_path))
    hit = face_pick.face_at(SID, [0.010, 0.010, 0.004], [0, 0, 1])
    kept, dropped = face_pick.verify(SID, [{"id": hit["id"], "label": "made up"}, {"id": hit["id"], "label": "again"},
                                           {"id": "Face_999", "label": "Face 999 · gone"}])
    assert [f["id"] for f in kept] == [hit["id"]] and kept[0]["label"] == hit["label"]
    assert kept[0]["sig"] == hit["sig"] and dropped == ["Face 999 · gone"]


def test_a_part_that_cannot_be_read_keeps_no_map_and_never_fails(store, monkeypatch, tmp_path):
    bad = tmp_path / "bad.step"
    bad.write_text("not a step file")
    facts = _checked(store, monkeypatch, bad)
    assert face_pick.FACT_KEY not in facts


def test_the_erasure_deletes_the_face_map():
    from meshpipeline.application import privacy_service
    assert face_pick.OBJECT_NAME in privacy_service._CHECK_RECORDS
