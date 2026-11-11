# Responsibility: Verify SURF-8..10 (cad/surface_clean, through cad/surface_model.read): the safe part of geometry
# clean-up on a surface file. Gaps between neighbouring faces are closed up a sewing ladder, never past the rule's
# limit unless the user stated a tolerance; a slot drawn in one face and two faces lying on each other are never
# sewn; a face written twice is left out; a pinhole is filled; what is left open is measured and said; a clean file
# is read exactly as before; and what kind of file it is is settled before any of it.
# Boundaries: small shapes drawn here with OpenCASCADE (tests/surface_fixtures); no mesh, no file.
from __future__ import annotations

import json

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import surface_fixtures as sx  # noqa: E402

from meshpipeline.cad import surface_clean as sc  # noqa: E402
from meshpipeline.cad import surface_model as sm  # noqa: E402

#: three 100 x 80 plates side by side: the outline of the whole panel
PANEL_RIM = 2.0 * (300.0 + 80.0)


def _read(shape, **kw):
    return sm.read(shape, mm_per_unit=1.0, **kw)


def _tol(shape=None) -> float:
    return _read(shape if shape is not None else sx.strip_of_plates())[0]["sew_tolerance_mm"]


def _free(reading) -> float:
    return sum(s["free_length_mm"] for s in reading["sheets"])


def test_a_clean_file_is_read_as_before_and_says_nothing_was_done():
    reading, sewn = _read(sx.strip_of_plates())
    assert reading["kind"] == "surface" and len(reading["sheets"]) == 1 and reading["faces"] == 3
    assert _free(reading) == pytest.approx(PANEL_RIM, rel=1e-6)
    c = reading["cleaned"]
    assert c["closed_edges"] == 0 and c["duplicates"] == 0 and c["pinholes"] == 0 and c["left_open"] == 0
    assert c["wider_gaps"] == 0 and c["refused"] == "" and c["limit_from"] == "rule"
    assert c["limit_mm"] == pytest.approx(sc.GAP_RULE * reading["sew_tolerance_mm"], rel=1e-5)
    assert sewn.names == ["Face_1", "Face_2", "Face_3"]


@pytest.mark.parametrize("move", [(0, 0, 3.0), (3.0, 0, 0), (1.5, 1.5, 1.5), (0, 2.0, 4.0)])
def test_a_face_written_a_few_tolerances_off_is_stitched_back(move):
    """A step, a gap on one side with an overlap on the other, a slide along the joint with a step: all closed by
    rule."""
    tol = _tol()
    shape = sx.strip_of_plates(move={1: tuple(k * tol for k in move)})
    reading, sewn = _read(shape)
    c = reading["cleaned"]
    assert reading["kind"] == "surface" and len(reading["sheets"]) == 1 and reading["faces"] == 3
    assert c["closed_edges"] == 2 and c["left_open"] == 0 and c["refused"] == ""
    assert c["closed_widest_mm"] <= c["limit_mm"] and c["rung_mm"] <= c["limit_mm"] * (1 + 1e-9)
    assert len(c["closed_at_mm"]) == 2
    # no free edge is lost or made but the two joints': the panel's own rim, within the few tolerances it moved
    assert _free(reading) == pytest.approx(PANEL_RIM, abs=12 * 3.0 * tol)
    assert sum(s["unmet_edges"] for s in reading["sheets"]) == 0
    assert sewn.names == ["Face_1", "Face_2", "Face_3"]


def test_the_ladder_takes_the_lowest_rung_that_closes_the_gap():
    tol = _tol()
    low, _ = _read(sx.strip_of_plates(move={1: (0, 0, 1.6 * tol)}))
    high, _ = _read(sx.strip_of_plates(move={1: (0, 0, 4.0 * tol)}))
    assert low["cleaned"]["rung_mm"] == pytest.approx(sc.RUNGS[0] * tol, rel=1e-3)
    assert high["cleaned"]["rung_mm"] == pytest.approx(sc.GAP_RULE * tol, rel=1e-3)
    assert low["cleaned"]["closed_widest_mm"] == pytest.approx(1.6 * tol, rel=0.02)


def test_a_gap_wider_than_the_rule_is_left_open_and_said_with_the_tolerance_that_closes_it():
    tol = _tol()
    shape = sx.strip_of_plates(move={1: (0, 0, 12.0 * tol)})
    reading, _sewn = _read(shape)
    c = reading["cleaned"]
    assert reading["kind"] == "surface" and len(reading["sheets"]) == 3 and c["closed_edges"] == 0
    assert c["wider_gaps"] == 2 and c["wider_widest_mm"] == pytest.approx(12.0 * tol, rel=0.02)
    assert len(c["wider_at_mm"]) == 2
    # ... and the user's own tolerance closes it
    said, _sewn = _read(shape, sew_mm=13.0 * tol)
    cs = said["cleaned"]
    assert len(said["sheets"]) == 1 and cs["closed_edges"] == 2 and cs["limit_from"] == "notes"
    assert cs["limit_mm"] == pytest.approx(13.0 * tol, rel=1e-5) and cs["wider_gaps"] == 0
    assert cs["closed_widest_mm"] == pytest.approx(12.0 * tol, rel=0.02)


def test_do_not_stitch_leaves_every_gap_as_drawn():
    tol = _tol()
    reading, _sewn = _read(sx.strip_of_plates(move={1: (0, 0, 3.0 * tol)}), stitch=False)
    assert reading["cleaned"]["stitch"] == "off" and "closed_edges" not in reading["cleaned"]
    assert len(reading["sheets"]) == 3 and reading["cleaned"]["left_open"] == sum(
        s["unmet_edges"] for s in reading["sheets"]) > 0


@pytest.mark.parametrize("slot_tols", [0.5, 2.0, 4.0, 20.0])
def test_a_slot_drawn_in_one_face_is_never_sewn_shut(slot_tols):
    plate, _ = _read(sx.plate(200.0, 100.0))
    tol = plate["sew_tolerance_mm"]
    shape = sx.plate_with_slot(slot=slot_tols * tol)
    want = 2.0 * (200.0 + 100.0) + 2.0 * (60.0 + slot_tols * tol)
    for kw in ({}, {"sew_mm": 50.0 * tol}):
        reading, _sewn = _read(shape, **kw)
        assert reading["kind"] == "surface" and reading["faces"] == 1
        assert reading["cleaned"]["closed_edges"] == 0 and reading["cleaned"]["pinholes"] == 0
        assert _free(reading) == pytest.approx(want, rel=1e-6)
        assert reading["sheets"][0]["loop_count"] == 2


def test_a_face_written_twice_is_left_out_once():
    plates = sx.faces_of(sx.strip_of_plates())
    reading, sewn = _read(sx.compound([*plates, sx.copy_of(plates[1])]))
    assert reading["kind"] == "surface" and reading["faces"] == 3 and len(reading["sheets"]) == 1
    assert reading["cleaned"]["duplicates"] == 1 and reading["cleaned"]["duplicate_faces"] == ["Face_4"]
    assert _free(reading) == pytest.approx(PANEL_RIM, rel=1e-6)
    assert sum(s["area_mm2"] for s in reading["sheets"]) == pytest.approx(300.0 * 80.0, rel=1e-6)
    assert sewn.names == ["Face_1", "Face_2", "Face_3"]
    # a one-face file written twice is still that one sheet, not a closed pocket
    one = sx.plate_with_hole()
    twice, _sewn = _read(sx.compound([one, sx.copy_of(one)]))
    assert twice["kind"] == "surface" and twice["faces"] == 1 and twice["cleaned"]["duplicates"] == 1
    assert _free(twice) == pytest.approx(sx.perimeter_of_plate_with_hole(), rel=1e-4)


def test_a_smaller_face_lying_on_another_is_no_duplicate():
    kept, dropped = sc.without_duplicates([sx.rect(0, 0, 100, 80), sx.rect(0, 0, 50, 80), sx.rect(0, 0, 100, 80, z=1.0)],
                                          0.01)
    assert kept == [0, 1, 2] and dropped == []


@pytest.mark.parametrize("off_tols", [2.0, 4.0])
def test_two_faces_lying_on_each_other_are_never_sewn_into_a_pocket(off_tols):
    """A copy of the sheet a few tolerances above it: every edge has a partner within the rule, and none is joined."""
    one = sx.plate_with_hole()
    tol = _tol(one)
    shape = sx.compound([one, sx.moved(sx.copy_of(one), (0, 0, off_tols * tol))])
    # by its shape alone the file is two skins with a crack all round (SURF-1, as before); said to be a shell, it
    # is two sheets
    assert _read(shape)[0]["kind"] == "gap"
    reading, _sewn = _read(shape, said_shell=True)
    assert reading["kind"] == "surface" and len(reading["sheets"]) == 2 and reading["faces"] == 2
    assert reading["cleaned"]["closed_edges"] == 0
    assert "lying on each other" in reading["cleaned"]["refused"]
    assert _free(reading) == pytest.approx(2.0 * sx.perimeter_of_plate_with_hole(), rel=1e-4)


def test_a_pinhole_is_filled_and_a_hole_is_kept_unless_the_user_names_its_size():
    plate, _ = _read(sx.plate(200.0, 100.0))
    tol = plate["sew_tolerance_mm"]
    rim = 2.0 * (200.0 + 100.0)
    pin, _sewn = _read(sx.plate_with_hole(r=1.0 * tol))                 # 2 tolerances across
    assert pin["cleaned"]["pinholes"] == 1 and pin["cleaned"]["pinhole_widest_mm"] == pytest.approx(2.0 * tol, rel=0.02)
    assert pin["sheets"][0]["loop_count"] == 1 and _free(pin) == pytest.approx(rim, rel=1e-6)
    assert pin["sheets"][0]["area_mm2"] == pytest.approx(200.0 * 100.0, rel=1e-6)
    hole, _sewn = _read(sx.plate_with_hole(r=5.0))
    assert hole["cleaned"]["pinholes"] == 0 and hole["sheets"][0]["loop_count"] == 2
    asked, _sewn = _read(sx.plate_with_hole(r=5.0), holes_mm=12.0)
    assert asked["cleaned"]["pinholes"] == 1 and asked["cleaned"]["holes_from"] == "notes"
    assert asked["sheets"][0]["loop_count"] == 1 and _free(asked) == pytest.approx(rim, rel=1e-6)
    under, _sewn = _read(sx.plate_with_hole(r=5.0), holes_mm=8.0)        # 10 mm across is over 8
    assert under["cleaned"]["pinholes"] == 0 and under["sheets"][0]["loop_count"] == 2


def test_a_gap_wide_against_the_narrow_face_beside_it_is_not_closed_by_rule():
    """A 0.3 mm strip between two plates, written 3 tolerances off: the rule will not reach a third of the way across
    it; the user's tolerance does."""
    faces = [sx.rect(0, 0, 100, 80), sx.rect(100, 0, 100.3, 80), sx.rect(100.3, 0, 200, 80)]
    tol = _tol(sx.compound(faces))
    shape = sx.compound([faces[0], sx.moved(faces[1], (0, 0, 3.0 * tol)), faces[2]])
    reading, _sewn = _read(shape)
    assert reading["cleaned"]["closed_edges"] == 0 and "narrow face" in reading["cleaned"]["refused"]
    assert len(reading["sheets"]) == 3 and reading["cleaned"]["left_open"] > 0
    said, _sewn = _read(shape, sew_mm=4.0 * tol)
    assert said["cleaned"]["closed_edges"] == 2 and len(said["sheets"]) == 1


def test_what_kind_of_file_it_is_is_read_before_any_clean_up():
    """A closed skin with a crack is still a closed skin with a crack (SURF-1): nothing is stitched to decide it."""
    tol = _tol(sx.tray())
    cracked, _sewn = _read(sx.skin_with_crack(3.0 * tol))
    assert cracked["kind"] == "gap" and "cleaned" not in cracked
    assert sum(s["unmet_edges"] for s in cracked["sheets"]) == 8
    # ... and once the user says it is a shell, its crack is a gap like any other
    said, _sewn = _read(sx.skin_with_crack(3.0 * tol), said_shell=True)
    assert said["kind"] == "surface" and said["cleaned"]["closed_edges"] == 4 and said["cleaned"]["left_open"] == 0


def test_the_same_file_twice_reads_the_same():
    tol = _tol()
    shape = sx.strip_of_plates(move={1: (0.5 * tol, 0, 3.0 * tol)})
    a, _ = _read(shape)
    b, _ = _read(shape)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_one_rung_reports_what_it_joined_and_why_it_is_refused():
    tol = _tol()
    base = sm.sew(sx.strip_of_plates(move={1: (0, 0, 3.0 * tol)}))
    got, rec = sc.try_rung(base, 2.0 * base.tol)
    assert got is None and rec["joined"] == 0 and rec["refused"] == ""            # nothing within two tolerances
    got, rec = sc.try_rung(base, 5.0 * base.tol)
    assert got is not None and rec["joined"] == 2 and rec["widest"] == pytest.approx(3.0 * tol, rel=0.02)
    assert len(got.sheets) == 1 and len(got.faces) == 3


def _tmeet(lift: float = 0.0, slide: float = 0.0):
    """A plate beside two half plates: the half plates' shared corner lands in the middle of the plate's edge (a
    T-meet), the half plates written `lift` above and `slide` across."""
    halves = [sx.moved(f, (slide, 0, lift)) for f in (sx.rect(100, 0, 200, 40), sx.rect(100, 40, 200, 80))]
    return sx.compound([sx.rect(0, 0, 100, 80), *halves])


@pytest.mark.parametrize(("lift", "slide"), [(3.0, 0.0), (0.0, 3.0), (2.0, 2.0)])
def test_a_t_meet_written_off_its_neighbour_is_stitched(lift, slide):
    """The coordinator's case: the sewing cuts the long edge where the half plates' corner meets it, and the pieces
    it returns are not in its own map. They are traced to their face by where they lie, so the joins are judged and
    taken rather than the rung refused ("a joined edge could not be traced to one face")."""
    tol = _tol(_tmeet())
    reading, _sewn = _read(_tmeet(lift * tol, slide * tol))
    c = reading["cleaned"]
    assert reading["kind"] == "surface" and len(reading["sheets"]) == 1 and reading["faces"] == 3
    assert c["closed_edges"] >= 2 and c["left_open"] == 0 and c["wider_gaps"] == 0
    assert "could not be traced" not in c["refused"]
    assert _free(reading) == pytest.approx(2.0 * (200.0 + 80.0), abs=12 * 3.0 * tol)


def test_a_stated_tolerance_never_leaves_more_open_than_the_rule():
    """A gap of 12 tolerances and one of 3: the rule closes the 3, a stated 15 closes both - and whatever is stated,
    no more is left open than the rule leaves, and what is said is consistent with what was closed."""
    tol = _tol()
    shape = sx.strip_of_plates(move={0: (0, 0, 3.0 * tol), 2: (0, 0, 12.0 * tol)})
    rule, _ = _read(shape)
    assert rule["cleaned"]["closed_edges"] == 1 and len(rule["sheets"]) == 2
    for stated in (4.0, 8.0, 15.0, 40.0):
        got, _ = _read(shape, sew_mm=stated * tol)
        c = got["cleaned"]
        assert len(got["sheets"]) <= len(rule["sheets"])
        assert _free(got) <= _free(rule) + 4.0 * tol
        # gaps said as wider are wider than what was applied, and a tolerance is offered only when it closes them
        if c["wider_gaps"]:
            assert c["wider_widest_mm"] > c["applied_mm"]
        if c["advice_mm"]:
            again, _ = _read(shape, sew_mm=c["advice_mm"])
            assert again["cleaned"]["wider_gaps"] == 0 and len(again["sheets"]) < len(got["sheets"])
    assert len(_read(shape, sew_mm=15.0 * tol)[0]["sheets"]) == 1


def test_a_rung_with_one_join_that_fails_still_takes_the_others():
    """A slot 2 tolerances wide drawn in the middle plate (its two edges are one face's: never sewn) and the right
    plate written 3 tolerances off: the slot's join is refused, the right plate's gap is still closed."""
    tol = _tol()
    slotted = sx.moved(sx.plate_with_slot(100.0, 80.0, slot=2.0 * tol, run=40.0), (100.0, 0, 0))
    shape = sx.compound([sx.rect(0, 0, 100, 80), slotted, sx.moved(sx.rect(200, 0, 300, 80), (0, 0, 3.0 * tol))])
    reading, _sewn = _read(shape)
    c = reading["cleaned"]
    assert c["closed_edges"] == 1
    assert len(reading["sheets"]) == 1 and reading["faces"] == 3
    # the slot is still open: the panel's rim and the slot's outline
    assert _free(reading) == pytest.approx(2.0 * (300.0 + 80.0) + 2.0 * (40.0 + 2.0 * tol), abs=12 * 3.0 * tol)
