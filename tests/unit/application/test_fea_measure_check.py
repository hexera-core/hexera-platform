# Responsibility: Hold the FEA measurement's place in the geometry check to its contract: an FEA
# part's naming step measures the upload (CAD faces or triangles) and keeps the record in the
# check's facts for the FEA plan; a part it cannot measure says why and the check still lands; the
# stage is served the thinnest wall and the hot spots (shown, never edited); and the record re-reads
# in the unit the user confirmed.
# Boundaries: the store and the fetch are stood in for; parts are built in the test.
from __future__ import annotations

import shutil

import pytest

from meshpipeline.application import fea_check
from meshpipeline.application import geometry_check as gc
from meshpipeline.cad.fea_measure import FACT_KEY

SOURCE = {"source_id": "src-1", "owner_id": "owner-1", "object_key": "uploads/part", "sha256": "0" * 64,
          "size_bytes": 1, "original_filename": "part.step", "suffix_hint": ".step"}


def _stored(path, suffix):
    return {"status": gc.STATUS_SCOUTED, "source": {**SOURCE, "suffix_hint": suffix,
                                                     "original_filename": f"part{suffix}"},
            "facts": {"input_kind": "solid-body", "flow": "external", "openings": [], "notes": [],
                      "size_mm": [200.0, 100.0, 5.0], "solids": 1, "scale_to_m": 0.001, "unit_assumed": False},
            "snapshots": []}


def _serve(monkeypatch, path):
    def fetch(ref, work):
        dest = work / f"upload{path.suffix}"
        shutil.copyfile(path, dest)
        return dest
    monkeypatch.setattr(gc, "_fetch", fetch)


def test_a_cad_upload_is_measured_on_its_faces(monkeypatch, tmp_path):
    pytest.importorskip("OCP.BRepPrimAPI")
    from tests import fea_fixtures as fx

    step = fx.write_step(fx.audit_plate_with_hole(), tmp_path / "plate.step")
    _serve(monkeypatch, step)
    stored = _stored(step, ".step")
    rec = gc.fea_measurements(stored, stored["facts"], None)
    assert rec["measured"] and rec["basis"] == "cad" and rec["scale_to_m"] == 0.001
    assert rec["thickness"]["min_mm"] == pytest.approx(5.0, rel=0.005)
    assert [(h["type"], h["radius_mm"]) for h in rec["hot_spots"]] == [("hole", pytest.approx(10.0))]


def test_a_triangle_upload_is_measured_in_the_checks_unit(monkeypatch, tmp_path):
    pytest.importorskip("OCP.BRepPrimAPI")
    from tests import fea_fixtures as fx

    from meshpipeline.cad.stl_io import write_stl_binary

    # the plate written in METRES, as the check read it (scale_to_m 1): lengths come back in mm
    tris = fx.stl_triangles(fx.plate_with_hole()) / 1000.0
    stl = tmp_path / "plate.stl"
    write_stl_binary(stl, [(t[0].tolist(), t[1].tolist(), t[2].tolist()) for t in tris])
    _serve(monkeypatch, stl)
    stored = _stored(stl, ".stl")
    stored["facts"]["scale_to_m"] = 1.0
    rec = gc.fea_measurements(stored, stored["facts"], None)
    assert rec["measured"] and rec["basis"] == "triangles" and rec["scale_to_m"] == 1.0
    assert rec["thickness"]["min_mm"] == pytest.approx(4.0, rel=0.01)
    assert rec["hot_spots"][0]["radius_mm"] == pytest.approx(7.5, rel=0.02)


def test_a_part_that_cannot_be_measured_says_why_and_the_check_lands(monkeypatch):
    def broken(ref, work):
        raise OSError("the store is down")
    monkeypatch.setattr(gc, "_fetch", broken)
    stored = _stored(None, ".step")
    rec = gc.fea_measurements(stored, stored["facts"], None)
    assert rec["measured"] is False and "could not be measured" in rec["reason"]
    assert fea_check.measure_view({FACT_KEY: rec}) == {"measured": False, "reason": rec["reason"]}


def test_the_naming_step_keeps_the_record_in_the_facts(monkeypatch):
    stored = _stored(None, ".step")
    landed: dict = {}
    monkeypatch.setattr(gc, "_wait_for_scout", lambda sid: stored)
    monkeypatch.setattr(gc, "store_status", lambda sid, status, step, **kw: landed.update(status=status, **kw) or landed)
    monkeypatch.setattr(gc, "fea_measurements", lambda s, facts, interp: {"measured": True, "hot_spots": []})
    out = gc._name(session_id="s1", owner_id="o", purpose_text="stress testing", interpretation=None)
    assert out["status"] == gc.STATUS_READY
    assert out["facts"][FACT_KEY] == {"measured": True, "hot_spots": []}


def test_the_stage_is_served_the_wall_and_the_hot_spots():
    pytest.importorskip("OCP.BRepPrimAPI")
    from tests import fea_fixtures as fx

    from meshpipeline.cad.fea_measure import measure_cad

    rec = measure_cad(fx.audit_l_bracket(), mm_per_unit=1.0)
    rec.update(measured=True, scale_to_m=0.001)
    view = fea_check.fea_view({"solids": 1, FACT_KEY: rec})["measure"]
    assert view["measured"] and view["thickness_min_mm"] == pytest.approx(8.0, rel=0.005)
    assert view["summary"] == "2 holes and 1 fillet"
    assert view["more"] == 0
    assert {h["type"] for h in view["hot_spots"]} == {"hole", "fillet"}
    assert all(set(h) <= {"id", "type", "shape", "radius_mm", "angle_deg", "length_mm", "depth_mm", "faces",
                          "at_mm", "capped"} for h in view["hot_spots"])
    # nothing measured yet: nothing shown
    assert fea_check.fea_view({"solids": 1})["measure"] is None


def test_the_hot_spots_are_said_in_plain_words():
    assert fea_check.hot_spot_words({}) == "no hot spots"
    assert fea_check.hot_spot_words({"hole": 1}) == "1 hole"
    assert fea_check.hot_spot_words({"sharp_corner": 2, "fillet": 1, "notch": 1}) == \
        "1 fillet, 1 notch or groove and 2 sharp inside corners"


def test_the_record_re_reads_in_the_confirmed_unit():
    rec = {"scale_to_m": 0.001, "thickness": {"min_mm": 5.0, "at_mm": [1.0, 2.0, 3.0]},
           "faces": {"Face_1": {"area_mm2": 10.0, "thickness_min_mm": 5.0}},
           "hot_spots": [{"type": "hole", "radius_mm": 10.0, "span_deg": 360.0},
                         {"type": "sharp_corner", "angle_deg": 90.0, "length_mm": 30.0}]}
    inch = fea_check.in_unit(rec, 0.0254)
    assert inch["scale_to_m"] == 0.0254
    assert inch["thickness"]["min_mm"] == pytest.approx(127.0)
    assert inch["thickness"]["at_mm"] == [pytest.approx(25.4), pytest.approx(50.8), pytest.approx(76.2)]
    assert inch["faces"]["Face_1"]["area_mm2"] == pytest.approx(6451.6)
    assert inch["hot_spots"][0]["radius_mm"] == pytest.approx(254.0)
    assert inch["hot_spots"][0]["span_deg"] == 360.0 and inch["hot_spots"][1]["angle_deg"] == 90.0
    assert fea_check.in_unit(rec, 0.001) == rec and fea_check.in_unit(None, 0.001) is None
