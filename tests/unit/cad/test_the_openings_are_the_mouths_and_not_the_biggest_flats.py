"""A flat face that is not at a mouth of the part's passage is not an opening.

WHY THIS EXISTS, measured end to end on 2026-10-29. An 8 x 6 x 2 mm coupon of the two-scale plate
with two 0.8 mm bores through its lid reached the builder with this brief:

    the `inlet` patch, which is 7.82 mm across and located near (4, 3, 0) mm, and exits through
    the `outlet` patch, which is 7.71 mm across and located near (4, 3, 2) mm

7.82 and 7.71 mm are the equal-area diameters of the plate's two LARGEST FLAT FACES, top and
bottom. Both are rim-free and on an extremity, so both read as openings; the count reached two;
and `map_openings` returned early on "the faces' exact openings always win when there are two or
more of them", so the bores were never looked at. The full 24 x 16 x 3 mm plate got this right
only because its faces happened to give NONE.

THE COUNT IS NOT THE QUESTION. Whether a face is where the coolant leaves is, and the inside-space
map is what knows where the mouths are: it had this part as one passage with two mouths. So the
faces keep their openings when they LINE UP with the mapped mouths - which is the pipe whose two
ends genuinely are faces, measured exactly off the B-rep - and are replaced when they do not.
"""
import pytest

from meshpipeline.application.geometry_check import SAME_OPENING, map_openings


def _hole(centre_mm, width_mm=0.8, passage=True):
    """A hole as `skin_holes` records it (cad/open_ends)."""
    return {"kind": "bore", "shape": "rectangle", "passage": passage,
            "centroid_m": [v / 1000.0 for v in centre_mm], "centroid_mm": list(centre_mm),
            "normal": [0.0, 0.0, 1.0], "area_mm2": width_mm ** 2,
            "diameter_mm": round(width_mm * 1.128, 4),
            "width_mm": width_mm, "height_mm": width_mm, "confidence": 0.85}


def _face_port(centre_mm, name, role, d_mm):
    """An opening as the scout reads it off a B-rep face."""
    return {"id": 1 if role == "inlet" else 2, "name": name, "role": role,
            "centroid_m": [v / 1000.0 for v in centre_mm], "centroid_mm": list(centre_mm),
            "normal": [0.0, 0.0, 1.0], "area_mm2": 3.1416 * (d_mm / 2) ** 2,
            "diameter_mm": d_mm}


#: THE COUPON. Two 0.8 mm bores through the lid at z = 2, and two 8 x 6 mm flats at z = 0 and 2.
BORES = [_hole((6.0, 5.0, 2.0)), _hole((2.0, 1.0, 2.0))]
BIG_FLATS = [_face_port((4.0, 3.0, 0.0), "inlet", "inlet", 7.82),
             _face_port((4.0, 3.0, 2.0), "outlet", "outlet", 7.71)]
INSIDE = {"rule": "INS-1", "has_inside": True, "passages": 1, "mouths_per_passage": [2]}


def _facts(openings, holes, inside=None):
    return {"openings": [dict(o) for o in openings], "holes": [dict(h) for h in holes],
            "inside": dict(inside if inside is not None else INSIDE), "notes": []}


#   -- the coupon --------------------------------------------------------------------------------

def test_the_bores_beat_the_two_biggest_flats():
    f = _facts(BIG_FLATS, BORES)
    map_openings(f)
    assert [o["role"] for o in f["openings"]] == ["inlet", "outlet"]
    assert [round(o["area_mm2"], 4) for o in f["openings"]] == [0.64, 0.64], (
        "0.64 mm2 is a 0.8 mm bore; 48 mm2 would be the plate's face")
    assert all(o.get("from_map") for o in f["openings"])


def test_it_says_why_the_faces_were_put_aside():
    f = _facts(BIG_FLATS, BORES)
    map_openings(f)
    assert any("not at the mouths of its inside passage" in n for n in f["notes"])
    assert any("inside-space map found 2 mouth(s)" in n for n in f["notes"])


def test_the_mouth_positions_are_carried_not_reinvented():
    f = _facts(BIG_FLATS, BORES)
    map_openings(f)
    got = sorted(tuple(o["centroid_mm"]) for o in f["openings"])
    assert got == [(2.0, 1.0, 2.0), (6.0, 5.0, 2.0)]


#   -- what must not change ----------------------------------------------------------------------

def test_a_pipe_whose_ends_ARE_faces_keeps_its_exact_faces():
    """The commonest internal-flow part there is. The faces are measured off the B-rep and the
    mouths off the DRAWN SKIN - "close, not exact" - so replacing them would lose precision for
    nothing."""
    ends = [_face_port((0.0, 0.0, 0.0), "inlet", "inlet", 10.0),
            _face_port((0.0, 0.0, 50.0), "outlet", "outlet", 10.0)]
    holes = [_hole((0.0, 0.0, 0.0), width_mm=9.0), _hole((0.0, 0.0, 50.0), width_mm=9.0)]
    f = _facts(ends, holes)
    map_openings(f)
    assert [round(o["diameter_mm"], 1) for o in f["openings"]] == [10.0, 10.0]
    assert not any(o.get("from_map") for o in f["openings"])
    assert f["notes"] == [], "nothing happened, so nothing is said"


def test_a_face_a_little_off_its_mouth_is_still_that_mouth():
    """The two are measured differently and do not agree to the micron. SAME_OPENING is the
    tolerance, as a multiple of the mouth's own width."""
    off = (SAME_OPENING - 0.5) * 0.8                      # inside the bar, in mm
    ends = [_face_port((off, 0.0, 0.0), "inlet", "inlet", 10.0),
            _face_port((0.0, 0.0, 50.0), "outlet", "outlet", 10.0)]
    holes = [_hole((0.0, 0.0, 0.0)), _hole((0.0, 0.0, 50.0))]
    f = _facts(ends, holes)
    map_openings(f)
    assert not any(o.get("from_map") for o in f["openings"]), f"{off} mm off is the same opening"


def test_a_part_whose_faces_gave_none_is_unchanged_in_behaviour():
    """The full plate's case, and the reason this ever worked: no face openings at all."""
    f = _facts([], BORES)
    map_openings(f)
    assert len(f["openings"]) == 2
    assert all(o.get("from_map") for o in f["openings"])


def test_one_mapped_mouth_is_not_a_flow_path_and_nothing_is_replaced():
    f = _facts(BIG_FLATS, [_hole((6.0, 5.0, 2.0))])
    map_openings(f)
    assert [round(o["diameter_mm"], 2) for o in f["openings"]] == [7.82, 7.71]


def test_a_part_with_no_inside_is_left_alone():
    """Having no cavity is the normal state of a solid body."""
    f = _facts(BIG_FLATS, BORES, inside={"rule": "INS-1", "has_inside": False})
    map_openings(f)
    assert [round(o["diameter_mm"], 2) for o in f["openings"]] == [7.82, 7.71]


def test_a_part_the_map_could_not_read_is_left_alone():
    f = _facts(BIG_FLATS, BORES, inside={"rule": "INS-1", "mapped": False})
    map_openings(f)
    assert [round(o["diameter_mm"], 2) for o in f["openings"]] == [7.82, 7.71]


def test_a_side_pocket_s_mouth_is_proposed_as_not_an_opening():
    """It is a dead end. Naming it an outlet is a flow path out of a blind hole."""
    f = _facts([], [*BORES, _hole((4.0, 3.0, 2.0), passage=False)])
    map_openings(f)
    roles = [o["role"] for o in f["openings"]]
    assert roles.count("not_an_opening") == 1
    assert roles.count("inlet") == 1 and roles.count("outlet") == 1


@pytest.mark.parametrize("bad", [
    {"centroid_m": []}, {"centroid_m": [1.0, 2.0]},
])
def test_an_opening_with_no_usable_centre_does_not_crash_the_comparison(bad):
    ports = [{**BIG_FLATS[0], **bad}, BIG_FLATS[1]]
    f = _facts(ports, BORES)
    map_openings(f)                       # it simply is not at a mouth
    assert all(o.get("from_map") for o in f["openings"])
