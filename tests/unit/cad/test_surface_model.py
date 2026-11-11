# Responsibility: Verify SURF-1..3 (cad/surface_model): a file that is a surface is told from a solid, from faces that
# close and from a closed skin with a gap, by its measured open share; the user's words settle a file under the
# share; faces written unsewn are sewn within the clean check's tolerance with every free edge left free; an edge
# that does not meet its neighbour is counted, never closed; and the record and surface it writes are the ones the
# shell mesher reads (one part per sheet, one mid-face and group per CAD face).
# Boundaries: small shapes drawn here with OpenCASCADE (tests/surface_fixtures); no mesh, no file but the two written.
from __future__ import annotations

import json
import math

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import surface_fixtures as sx  # noqa: E402

from meshpipeline.cad import surface_model as sm  # noqa: E402


def _read(shape, **kw):
    return sm.read(shape, mm_per_unit=1.0, **kw)


def test_a_sheet_is_a_surface_and_its_free_edges_are_its_own():
    reading, sewn = _read(sx.plate_with_hole())
    assert reading["kind"] == "surface" and sewn is not None
    (sheet,) = reading["sheets"]
    assert sheet["free_length_mm"] == pytest.approx(sx.perimeter_of_plate_with_hole(), rel=1e-4)
    assert sheet["loop_count"] == 2 and sheet["unmet_edges"] == 0 and not sheet["closed"]
    # the rim spans the plate and the hole spans its own disc: the whole area and a little more
    assert reading["open_share"] > 1.0
    assert sheet["part"] == "Part_1" and sheet["faces"] == ["Face_1"]


def test_a_bent_sheet_a_tray_and_an_open_tube_are_surfaces():
    for shape, faces in ((sx.bracket(), 3), (sx.tray(), 5), (sx.open_tube(10.0, 300.0), 1)):
        reading, _sewn = _read(shape)
        assert reading["kind"] == "surface", reading["why"]
        assert reading["faces"] == faces and len(reading["sheets"]) == 1
        assert reading["open_share"] > sm.GAP_SHARE


def test_a_solid_and_faces_that_close_are_not_its_business():
    reading, sewn = _read(sx.box_solid())
    assert reading["kind"] == "solid" and sewn is None and reading["sheets"] == []
    reading, sewn = _read(sx.closed_skin())
    assert reading["kind"] == "closed" and sewn is None
    # ... written unsewn too: the clean check's sewing closes them
    assert _read(sx.unsewn(sx.closed_skin()))[0]["kind"] == "closed"


def test_a_closed_skin_with_a_small_hole_is_a_gap_unless_a_shell_was_asked_for():
    shape = sx.skin_with_pinhole()
    reading, sewn = _read(shape)
    assert reading["kind"] == "gap" and sewn is not None
    assert 0 < reading["open_share"] <= sm.GAP_SHARE
    assert "nearly close" in reading["why"]
    said, sewn = _read(shape, said_shell=True)
    assert said["kind"] == "surface" and sewn is not None and "asked for" in said["why"]


def test_a_seam_that_does_not_sew_is_a_crack_not_an_opening():
    reading, _sewn = _read(sx.skin_with_crack(0.0))
    assert reading["kind"] == "closed"
    tol = reading_tolerance = _read(sx.tray())[0]["sew_tolerance_mm"]
    assert reading_tolerance > 0
    # the lid lifted three sew tolerances: every free edge lies along a neighbour, so the loops span nothing
    cracked, _sewn = _read(sx.skin_with_crack(3.0 * tol))
    assert cracked["kind"] == "gap" and cracked["open_share"] == 0.0
    assert sum(s["unmet_edges"] for s in cracked["sheets"]) == 8
    assert all(lp["seam"] for s in cracked["sheets"] for lp in s["loops"])
    # ... and a lid lifted clear of its neighbours is a real opening again (a tray and a loose plate)
    apart, _sewn = _read(sx.skin_with_crack(100.0 * tol))
    assert apart["kind"] == "surface" and sum(s["unmet_edges"] for s in apart["sheets"]) == 0


def test_faces_written_unsewn_sew_into_the_same_sheet(tmp_path):
    whole, _s = _read(sx.bracket())
    loose, sewn = _read(sx.unsewn(sx.bracket()))
    assert loose["kind"] == "surface" and len(loose["sheets"]) == 1
    assert loose["sheets"][0]["free_length_mm"] == pytest.approx(whole["sheets"][0]["free_length_mm"], rel=1e-6)
    assert loose["sheets"][0]["shared_edges"] == whole["sheets"][0]["shared_edges"] == 2
    # and an IGES copy (trimmed surfaces, no shell) reads the same
    from meshpipeline.cad.scout import read_cad

    igs = read_cad(sx.write_iges(sx.bracket(), tmp_path / "bracket.igs"))
    back, _s = _read(igs)
    assert back["kind"] == "surface" and len(back["sheets"]) == 1
    assert back["sheets"][0]["free_length_mm"] == pytest.approx(whole["sheets"][0]["free_length_mm"], rel=1e-4)


def test_separate_sheets_are_separate_parts():
    shape = sx.compound([sx.bracket(), sx.moved(sx.plate_with_hole(), (0, 0, 200))])
    reading, _sewn = _read(shape)
    assert reading["kind"] == "surface"
    assert [s["part"] for s in reading["sheets"]] == ["Part_1", "Part_2"]
    assert [len(s["faces"]) for s in reading["sheets"]] == [3, 1]
    assert reading["sheets"][1]["faces"] == ["Face_4"]


def test_lengths_follow_the_files_unit():
    mm, _s = sm.read(sx.plate(), mm_per_unit=1.0)
    m, _s = sm.read(sx.plate(), mm_per_unit=1000.0)
    assert m["sheets"][0]["free_length_mm"] == pytest.approx(1000.0 * mm["sheets"][0]["free_length_mm"])
    assert m["sheets"][0]["area_mm2"] == pytest.approx(1e6 * mm["sheets"][0]["area_mm2"])
    assert m["open_share"] == pytest.approx(mm["open_share"])


def test_the_record_is_the_one_the_shell_mesher_reads(tmp_path):
    from OCP.BRep import BRep_Builder
    from OCP.BRepTools import BRepTools
    from OCP.TopoDS import TopoDS_Shape

    from meshpipeline.cad import midsurface as ms

    shape = sx.compound([sx.bracket(), sx.moved(sx.plate_with_hole(), (0, 0, 200))])
    reading, sewn = _read(shape)
    rec, surface = sm.record(reading, sewn, mm_per_unit=1.0, walls_mm={"Part_1": 1.5, "Part_2": 0.8},
                             given={"Part_1": True, "Part_2": True})
    sm.write(rec, surface, tmp_path)
    back = json.loads((tmp_path / ms.RECORD_FILE).read_text())
    assert back["source"] == "surface" and back["surface_model"]["kind"] == "surface"
    # one mid-face per CAD face, in the surface's own order; one wall pair and one body per sheet
    assert back["surface_faces"] == ["Mid_1", "Mid_2", "Mid_3", "Mid_4"]
    assert [m["faces"] for m in back["mids"]] == [["Face_1"], ["Face_2"], ["Face_3"], ["Face_4"]]
    assert [(p["id"], p["thickness_mm"], p["shell"]) for p in back["pairs"]] == [("P1", 1.5, True), ("P2", 0.8, True)]
    assert [m["pair"] for m in back["mids"]] == ["P1", "P1", "P1", "P2"]
    assert [b["verdict"] for b in back["bodies"]] == ["shell", "shell"]
    # each sheet's size is the square root of its area, as an extracted sheet's
    for p, s in zip(back["pairs"], reading["sheets"], strict=True):
        assert p["size_mm"] == pytest.approx(math.sqrt(s["area_mm2"]), rel=1e-5)
    assert back["checks"]["connected"] is True and [c["open_edges"] for c in back["sewn"]] == [0, 0]
    got = TopoDS_Shape()
    assert BRepTools.Read_s(got, str(tmp_path / ms.SURFACE_FILE), BRep_Builder())
    assert len(ms._faces(got)) == 4
    # the bracket's faces still share their two bend lines in the written surface
    assert len(ms._free_edge_list(got)) == reading["sheets"][0]["free_edges"] + reading["sheets"][1]["free_edges"]


def test_edges_that_do_not_meet_are_said_in_the_record():
    tol = _read(sx.tray())[0]["sew_tolerance_mm"]
    # left as drawn (the user said not to stitch): the gaps are in the record
    reading, sewn = _read(sx.skin_with_crack(3.0 * tol), said_shell=True, stitch=False)
    assert reading["kind"] == "surface"
    rec, _surface = sm.record(reading, sewn, mm_per_unit=1.0, walls_mm={s["part"]: 1.0 for s in reading["sheets"]})
    assert rec["checks"]["connected"] is False
    assert sum(c["open_edges"] for c in rec["sewn"]) == 8
    assert any("SURF-2" in n for n in rec["notes"])
    # by rule they are closed (SURF-8), and the record says that instead
    reading, sewn = _read(sx.skin_with_crack(3.0 * tol), said_shell=True)
    rec, _surface = sm.record(reading, sewn, mm_per_unit=1.0, walls_mm={s["part"]: 1.0 for s in reading["sheets"]})
    assert rec["checks"]["connected"] is True and sum(c["open_edges"] for c in rec["sewn"]) == 0
    assert any("SURF-8" in n and "4 gap(s)" in n for n in rec["notes"]) and not any("SURF-2" in n for n in rec["notes"])


def test_three_faces_at_one_line_are_sewn_into_a_junction():
    """SURF-2: a rib's foot on the line two plate faces share - the plain sewing joins two faces at an edge and
    leaves the third lying along it; sewn again with junctions allowed, the three share the edge."""
    reading, sewn = _read(sx.tee(split=True))
    assert reading["kind"] == "surface" and len(reading["sheets"]) == 1
    (sheet,) = reading["sheets"]
    assert sheet["junction_edges"] == 1 and sheet["unmet_edges"] == 0 and sheet["shared_edges"] == 0
    assert sheet["faces"] == ["Face_1", "Face_2", "Face_3"]
    # the free edges are the plate's rim and the web's three other sides
    assert sheet["free_length_mm"] == pytest.approx(2 * (200 + 100) + (200 + 2 * 40))
    rec, _surface = sm.record(reading, sewn, mm_per_unit=1.0, walls_mm={"Part_1": 2.0})
    assert rec["checks"]["connected"] is True and rec["sewn"][0]["junction_edges"] == 1


def test_a_rib_landing_inside_a_face_is_joined_by_scoring_that_face():
    """... and a foot that lands inside ONE face has no edge there to sew to: the faces are fused (the extractor's
    own way, SHELL-4), which scores the plate along the foot. Nothing moves and no area changes; the plate's two
    pieces are still one CAD face, one mid-face, one group."""
    reading, sewn = _read(sx.tee(split=False))
    assert reading["kind"] == "surface" and len(reading["sheets"]) == 1 and reading["faces"] == 2
    (sheet,) = reading["sheets"]
    assert sheet["faces"] == ["Face_1", "Face_2"] and sheet["unmet_edges"] == 0 and sheet["junction_edges"] == 1
    assert sheet["free_length_mm"] == pytest.approx(2 * (200 + 100) + (200 + 2 * 40))
    assert sheet["area_mm2"] == pytest.approx(200 * 100 + 200 * 40)
    assert sewn.names == ["Face_1", "Face_1", "Face_2"]
    rec, surface = sm.record(reading, sewn, mm_per_unit=1.0, walls_mm={"Part_1": 2.0})
    assert rec["surface_faces"] == ["Mid_1", "Mid_1", "Mid_2"] and [m["id"] for m in rec["mids"]] == ["Mid_1", "Mid_2"]
    assert rec["mids"][0]["area_mm2"] == pytest.approx(200 * 100) and rec["checks"]["connected"] is True
    from meshpipeline.cad import midsurface as ms

    assert len(ms._faces(surface)) == 3


def test_a_fuse_that_would_lose_or_change_a_face_is_not_kept(monkeypatch):
    """The imprint never changes what the file holds: when the fuse fails, the sewn faces stand and the edge is
    said as not meeting."""
    monkeypatch.setattr(sm, "imprint", lambda sewn: None)
    reading, sewn = _read(sx.tee(split=False))
    assert len(reading["sheets"]) == 2 and [s["unmet_edges"] for s in reading["sheets"]] == [0, 1]
    rec, _surface = sm.record(reading, sewn, mm_per_unit=1.0, walls_mm={"Part_1": 2.0, "Part_2": 2.0})
    assert rec["checks"]["connected"] is False and sum(c["open_edges"] for c in rec["sewn"]) == 1
    monkeypatch.undo()
    assert sm.imprint(sm.sew(sx.plate())) is None            # one face: nothing to fuse


def test_classify_is_pure_over_the_measured_sheets():
    sheet = {"area_mm2": 1000.0, "open_share": 0.5, "closed": False}
    assert sm.classify([sheet])[0] == "surface"
    assert sm.classify([{**sheet, "open_share": 0.004}])[0] == "gap"
    assert sm.classify([{**sheet, "open_share": 0.004}], said_shell=True)[0] == "surface"
    assert sm.classify([{**sheet, "open_share": 0.0, "closed": True}])[0] == "closed"
    # the share is the whole file's: a small sheet wide open beside a big skin that nearly closes
    assert sm.classify([{**sheet, "area_mm2": 100000.0, "open_share": 0.001},
                        {**sheet, "area_mm2": 2000.0, "open_share": 1.0}])[0] == "surface"
    assert sm.classify([])[0] == "empty"
