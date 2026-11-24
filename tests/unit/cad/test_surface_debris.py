# Responsibility: Verify SURF-16 (cad/surface_model.without_debris, through cad/surface_model.read): a loose sheet of a
# surface file smaller across than one element of the mesh is left out before anything is cleaned or sized, and said
# (how many faces, how small); a sheet as large as the user's asked size is kept; a part of one sheet is read as
# before. And SURF-12's search (cad/surface_mend.mend) measures each surface's open places once.
# Boundaries: small shapes drawn here with OpenCASCADE (tests/surface_fixtures); no mesh, no file.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import surface_fixtures as sx  # noqa: E402

from meshpipeline.cad import surface_model as sm  # noqa: E402


def _plate_and_slivers(n: int = 2):
    """A 200 x 120 mm plate and `n` stray pieces of two 0.3 x 0.15 mm faces each, 5 mm off it."""
    bits = []
    for i in range(n):
        x = 20.0 + 40.0 * i
        bits += [sx.moved(sx.rect(x, 10, x + 0.3, 10.15), (0, 0, 5.0)),
                 sx.moved(sx.rect(x, 10.15, x + 0.3, 10.3), (0, 0, 5.0))]
    return sx.compound([sx.rect(0, 0, 200, 120), *bits])


def test_stray_pieces_far_under_one_element_are_left_out_and_told():
    from meshpipeline.engines import surface_run as sr

    reading, sewn = sm.read(_plate_and_slivers(2), mm_per_unit=1.0, said_shell=True)
    assert reading["kind"] == "surface"
    assert len(reading["sheets"]) == 1 and sewn.names == ["Face_1"]
    loose = reading["cleaned"]["loose"]
    assert loose["sheets"] == 2 and loose["faces"] == 4 and loose["element_from"] == "run"
    assert loose["widest_mm"] == pytest.approx(0.3 * 2 ** 0.5, rel=1e-3)
    assert loose["area_mm2"] == pytest.approx(4 * 0.3 * 0.15, rel=1e-3)
    said = " ".join(sr.clean_words(reading["cleaned"]))
    assert "Left out 4 loose faces (2 stray pieces, the largest 0.42 mm across" in said


def test_a_piece_as_large_as_the_asked_size_is_kept():
    reading, sewn = sm.read(_plate_and_slivers(1), mm_per_unit=1.0, said_shell=True, element_mm=0.2)
    assert len(reading["sheets"]) == 2 and "loose" not in reading["cleaned"]
    assert len(sewn.faces) == 3


def test_a_small_patch_cut_off_by_a_gap_is_kept():
    # a 5 x 5 mm tab 0.1 mm off the plate's edge: under one element across, but a gap a tenth of its size away
    reading, _sewn = sm.read(sx.compound([sx.rect(0, 0, 200, 120), sx.rect(200.1, 50, 205.1, 55)]), mm_per_unit=1.0,
                             said_shell=True)
    assert "loose" not in reading["cleaned"]
    assert sum(s["area_mm2"] for s in reading["sheets"]) >= 200 * 120 + 25 - 1e-6


def test_a_part_of_one_sheet_reads_as_before():
    reading, _sewn = sm.read(sx.compound([sx.rect(0, 0, 100, 80), sx.rect(100, 0, 200, 80)]), mm_per_unit=1.0,
                             said_shell=True)
    assert len(reading["sheets"]) == 1 and "loose" not in reading["cleaned"]


def test_changing_the_surfaces_stops_by_the_clock_and_says_so(monkeypatch):
    # SURF-17: past the limit, the places not yet tried are left to the stitching and the mesh, and told
    from meshpipeline.cad import surface_mend as smd
    from meshpipeline.engines import surface_run as sr

    monkeypatch.setattr(smd, "MEND_LIMIT_S", -1.0)
    reading, _sewn = sm.read(sx.compound([sx.rect(0, 0, 100, 80), sx.rect(101.0, 0, 200, 80)]), mm_per_unit=1.0,
                             said_shell=True)
    m = reading["cleaned"]["mend"]
    assert m["closed"] == 0 and m["untried"] == 1 and m["stopped_s"] >= 0
    said = " ".join(sr.clean_words(reading["cleaned"]))
    assert "did not finish in" in said and "1 place was not tried" in said


def test_with_no_mesh_the_gaps_left_open_are_still_said():
    # SURF-8: a run that made no mesh says what the clean-up did and what is open, not only why it stopped
    from meshpipeline.engines import surface_run as sr

    cleaned = {"places_before": 14, "closed_places": 13, "closed_places_widest_mm": 0.25, "open_places": 1,
               "applied_mm": 0.269, "left_open": 0, "wider_gaps": 1, "wider_widest_mm": 0.54,
               "wider_narrowest_mm": 0.54, "advice_mm": 0.6, "stitch": "on", "mend": {"off": True}}
    said = sr.words({"made": False, "why": "the shell mesh ran past 15 minutes and was stopped",
                     "surface": {"kind": "surface", "cleaned": cleaned}})
    assert "could not be made: the shell mesh ran past 15 minutes" in said
    assert "Closed 13 of the 14 places" in said and "0.54 mm apart" in said
    assert '"stitch gaps up to 0.6 mm"' in said


def test_each_surface_s_open_places_are_measured_once(monkeypatch):
    calls: list = []
    real = sm.open_places

    def counted(sewn, **kw):
        calls.append(id(sewn))
        return real(sewn, **kw)

    from meshpipeline.cad import surface_mend as smd

    sewn = sm.sew(sx.compound([sx.rect(0, 0, 100, 80), sx.rect(101.0, 0, 200, 80)]))
    monkeypatch.setattr(sm, "open_places", counted)
    _got, rec = smd.mend(sewn, mm_per_unit=1.0, element_mm=5.0)
    assert rec["closed"] == 1
    assert len(calls) == len(set(calls))               # no surface searched twice (the memo holds each one)
    assert smd._MEMO == {}                             # and nothing is kept once the mend is done
