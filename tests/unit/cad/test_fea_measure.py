# Responsibility: Hold the FEA measurement (FEA build step 3) to parts of KNOWN geometry: the wall
# thickness, the hole, fillet, notch and groove radii, the sharp inside corners' angles and the body
# count must come out as drawn, within the tolerances stated below - read on the CAD faces, and
# read again from the same parts as triangles (an STL upload).
# Boundaries: parts are built in the test (tests/fea_fixtures.py); OpenCASCADE reads them, gmsh only
# confirms that Face_k is the deck's Face_k. No network, no store, no model.
from __future__ import annotations

import pytest

pytest.importorskip("OCP.BRepPrimAPI")

from tests import fea_fixtures as fx  # noqa: E402

from meshpipeline.cad import fea_features as ff  # noqa: E402
from meshpipeline.cad.fea_measure import measure_cad, measure_cad_file, measure_triangles  # noqa: E402

#: STATED TOLERANCES (relative, unless degrees):
#: on CAD faces, radii are the surface's own numbers; the wall is ray-traced on a fine tessellation
CAD_RADIUS, CAD_WALL, CAD_ANGLE_DEG, CAD_LENGTH = 1e-6, 0.005, 0.5, 0.005
#: on triangles (an STL), radii are fitted and the wall is traced on the file's own facets
STL_RADIUS, STL_WALL, STL_ANGLE_DEG = 0.02, 0.01, 2.0

#: name -> (builder, wall, [(type, shape, radius or angle)...], bodies)
KNOWN = {
    "audit L bracket": (fx.audit_l_bracket, 8.0,
                        [("fillet", "fillet", 6.0), ("hole", "hole", 4.25), ("hole", "hole", 4.25)], 1),
    "audit plate with hole": (fx.audit_plate_with_hole, 5.0, [("hole", "hole", 10.0)], 1),
    "audit cantilever": (fx.audit_cantilever, 20.0, [], 1),
    "plate with hole": (fx.plate_with_hole, 4.0, [("hole", "hole", 7.5)], 1),
    "filleted bracket": (fx.bracket, 6.0, [("fillet", "fillet", 4.0)], 1),
    "sharp bracket": (lambda: fx.bracket(None), 6.0, [("sharp_corner", "inside corner", 90.0)], 1),
    "stepped shaft with groove": (fx.stepped_shaft, 24.0,
                                  [("notch", "groove", 3.0), ("fillet", "fillet", 2.0)], 1),
    "V-notched bar": (fx.v_notched_bar, 20.0, [("sharp_corner", "inside corner", 60.0)], 1),
    "two blocks": (fx.two_blocks, 10.0, [], 2),
}


def _size(h: dict) -> float:
    return h["angle_deg"] if h["type"] == ff.SHARP_CORNER else h["radius_mm"]


def _check(rec: dict, wall: float, spots: list, bodies: int, *, radius_tol: float, wall_tol: float,
           angle_tol: float) -> None:
    assert rec["bodies"] == bodies
    assert rec["thickness"]["min_mm"] == pytest.approx(wall, rel=wall_tol)
    got = sorted((h["type"], h["shape"], _size(h)) for h in rec["hot_spots"])
    want = sorted(spots)
    assert [(t, s) for t, s, _ in got] == [(t, s) for t, s, _ in want], got
    for (t, _s, g), (_t, _w, w) in zip(got, want, strict=True):
        if t == ff.SHARP_CORNER:
            assert g == pytest.approx(w, abs=angle_tol)
        else:
            assert g == pytest.approx(w, rel=radius_tol)
    for h in rec["hot_spots"]:
        assert h["id"].startswith("H") and len(h["at_mm"]) == 3 and h["faces"]
        # Areen (20 Oct): sharp inside corners get a refinement CAP; nothing else does
        assert h["capped"] is (h["type"] == ff.SHARP_CORNER)


@pytest.mark.parametrize("name", list(KNOWN))
def test_the_cad_faces_measure_as_drawn(name):
    build, wall, spots, bodies = KNOWN[name]
    rec = measure_cad(build(), mm_per_unit=1.0)
    assert rec["basis"] == "cad" and rec["face_names"] == "Face_k" and rec["complete"]
    _check(rec, wall, spots, bodies, radius_tol=CAD_RADIUS, wall_tol=CAD_WALL, angle_tol=CAD_ANGLE_DEG)


@pytest.mark.parametrize("name", list(KNOWN))
def test_the_same_parts_as_triangles_measure_as_drawn(name):
    build, wall, spots, bodies = KNOWN[name]
    rec = measure_triangles(fx.stl_triangles(build()))
    assert rec["basis"] == "triangles" and rec["face_names"] == "Region_k"
    _check(rec, wall, spots, bodies, radius_tol=STL_RADIUS, wall_tol=STL_WALL, angle_tol=STL_ANGLE_DEG)


def test_a_coarse_stl_still_finds_the_hole_and_the_fillet():
    # an STL exported at a 17 degree facet angle: the hole is 21-sided, the fillet 6 facets
    plate = measure_triangles(fx.stl_triangles(fx.plate_with_hole(), 0.1, 0.3))
    assert [(h["type"], h["radius_mm"]) for h in plate["hot_spots"]] == [("hole", pytest.approx(7.5, rel=STL_RADIUS))]
    bracket = measure_triangles(fx.stl_triangles(fx.bracket(), 0.1, 0.3))
    assert [(h["type"], h["radius_mm"]) for h in bracket["hot_spots"]] == [("fillet", pytest.approx(4.0, rel=STL_RADIUS))]


def test_the_details_of_each_hot_spot_are_the_drawn_ones():
    rec = measure_cad(fx.bracket(None), mm_per_unit=1.0)
    corner = rec["hot_spots"][0]
    assert corner["length_mm"] == pytest.approx(fx.BRACKET["corner_length"], rel=CAD_LENGTH)
    # the corner runs along y at x = z = 6, and the thinner wall beside it is the base's 6 mm
    assert corner["at_mm"][0] == pytest.approx(6.0, abs=1e-6) and corner["at_mm"][2] == pytest.approx(6.0, abs=1e-6)
    assert corner["cap_basis_mm"] == pytest.approx(6.0, rel=CAD_WALL)
    plate = measure_cad(fx.audit_plate_with_hole(), mm_per_unit=1.0)
    hole = plate["hot_spots"][0]
    assert hole["depth_mm"] == pytest.approx(5.0, rel=CAD_LENGTH)
    assert hole["at_mm"][:2] == [pytest.approx(100.0), pytest.approx(50.0)]
    # the ligament from the hole to the plate's long side: (100 - 20) / 2
    assert hole["local_thickness_mm"] == pytest.approx(40.0, rel=CAD_WALL)
    shaft = measure_cad(fx.stepped_shaft(), mm_per_unit=1.0)
    groove = next(h for h in shaft["hot_spots"] if h["shape"] == "groove")
    assert groove["span_deg"] == pytest.approx(180.0, abs=CAD_ANGLE_DEG)
    assert groove["at_mm"][2] == pytest.approx(30.0, abs=3.0)


def test_every_face_has_its_wall_and_the_thinnest_says_where():
    rec = measure_cad(fx.audit_l_bracket(), mm_per_unit=1.0)
    assert len(rec["faces"]) == 11
    assert all("thickness_min_mm" in f and f["area_mm2"] > 0 for f in rec["faces"].values())
    assert rec["thickness"]["face"] in rec["faces"]
    assert rec["faces"][rec["thickness"]["face"]]["thickness_min_mm"] == rec["thickness"]["min_mm"]


def test_the_lengths_follow_the_files_unit():
    # the same bracket drawn in inches: every length 25.4 times, every angle the same
    mm = measure_cad(fx.bracket(None), mm_per_unit=1.0)
    inch = measure_cad(fx.bracket(None), mm_per_unit=25.4)
    assert inch["thickness"]["min_mm"] == pytest.approx(25.4 * mm["thickness"]["min_mm"], rel=1e-6)
    assert inch["hot_spots"][0]["angle_deg"] == mm["hot_spots"][0]["angle_deg"]
    assert inch["hot_spots"][0]["length_mm"] == pytest.approx(25.4 * mm["hot_spots"][0]["length_mm"], rel=1e-6)


def test_a_part_measures_the_same_twice():
    def plain(rec):
        return {k: v for k, v in rec.items() if k != "seconds"}

    assert plain(measure_cad(fx.audit_l_bracket(), mm_per_unit=1.0)) == \
        plain(measure_cad(fx.audit_l_bracket(), mm_per_unit=1.0))
    tris = fx.stl_triangles(fx.stepped_shaft())
    assert plain(measure_triangles(tris)) == plain(measure_triangles(tris))


def test_a_file_wound_inside_out_reads_the_same():
    tris = fx.stl_triangles(fx.bracket(None))
    rec = measure_triangles(tris[:, [0, 2, 1]])
    assert [(h["type"], h["angle_deg"]) for h in rec["hot_spots"]] == [("sharp_corner", pytest.approx(90.0, abs=STL_ANGLE_DEG))]


@pytest.mark.parametrize("build", [fx.audit_l_bracket, fx.stepped_shaft, fx.v_notched_bar])
def test_face_k_is_the_decks_face_k(tmp_path, build):
    """The deck's Face_k is gmsh's k-th face of the staged B-rep (engines/gmsh/abaqus_deck), which
    gmsh writes from the upload and reads back (engines/gmsh/gmsh_runner): the measurement's Face_k
    must be that same face."""
    gmsh = pytest.importorskip("gmsh")
    upload = fx.write_step(build(), tmp_path / "part.step")
    rec = measure_cad_file(upload, mm_per_unit=1.0)
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("stage")
        gmsh.model.occ.importShapes(str(upload))
        gmsh.model.occ.synchronize()
        gmsh.write(str(tmp_path / "geometry.step"))
        gmsh.model.remove()
        gmsh.model.add("driver")
        gmsh.model.occ.importShapes(str(tmp_path / "geometry.step"))
        gmsh.model.occ.synchronize()
        tags = sorted(t for _, t in gmsh.model.getEntities(2))
        assert len(tags) == len(rec["faces"])
        for k, tag in enumerate(tags, start=1):
            face = rec["faces"][f"Face_{k}"]
            assert face["area_mm2"] == pytest.approx(gmsh.model.occ.getMass(2, tag), rel=0.01)
            c = gmsh.model.occ.getCenterOfMass(2, tag)
            assert face["centroid_mm"] == [pytest.approx(v, abs=0.05) for v in c]
    finally:
        gmsh.finalize()


def test_a_sealed_cavity_is_a_wall_not_another_body():
    # a correctly wound STL of a hollow cube: the cavity's skin faces into the cavity, and stays so
    tris = fx.stl_triangles(fx.hollow_box())
    rec = measure_triangles(tris)
    assert rec["bodies"] == 1
    assert rec["thickness"]["min_mm"] == pytest.approx(fx.HOLLOW["thickness"], rel=STL_WALL)
    # the material wraps each of the cavity's 12 edges: 12 square inside corners, as on the CAD
    cad = measure_cad(fx.hollow_box(), mm_per_unit=1.0)
    for r in (rec, cad):
        assert [(h["type"], round(h["angle_deg"])) for h in r["hot_spots"]] == [(ff.SHARP_CORNER, 90)] * 12
    assert cad["bodies"] == 1 and cad["thickness"]["min_mm"] == pytest.approx(5.0, rel=CAD_WALL)


def test_a_fillet_split_into_two_faces_stays_a_fillet():
    rec = measure_cad(fx.split_fillet_bracket(), mm_per_unit=1.0)
    fillets = [h for h in rec["hot_spots"] if h["type"] != ff.SHARP_CORNER]
    assert [(h["type"], h["radius_mm"]) for h in fillets] == [("fillet", pytest.approx(4.0))]
    assert len(fillets[0]["faces"]) == 2 and fillets[0]["span_deg"] == pytest.approx(90.0, abs=1.0)


def test_a_fan_of_triangles_round_one_point_welds_without_blowing_up():
    import numpy as np

    # 20,000 thin triangles round one centre: that point repeats 20,000 times in the soup
    k = 20000
    ang = np.linspace(0, 2 * np.pi, k + 1)
    rim = np.column_stack([np.cos(ang), np.sin(ang), np.zeros(k + 1)]) * 10.0
    fan = np.stack([np.zeros((k, 3)), rim[:-1], rim[1:]], axis=1)
    from meshpipeline.cad.fea_measure import _weld

    xyz, t = _weld(fan.reshape(-1, 3), np.arange(3 * k).reshape(-1, 3))
    assert len(xyz) == k + 1 and t.shape == (k, 3)


def test_a_sharp_stretch_beside_a_fillet_is_still_a_corner_in_an_stl():
    # the base and the upright are one smooth region round the filleted half; the sharp half's
    # edge lies inside that region and is still a capped 90 degree inside corner, 15 mm long
    for rec in (measure_triangles(fx.stl_triangles(fx.part_filleted_bracket())),
                measure_cad(fx.part_filleted_bracket(), mm_per_unit=1.0)):
        # (the fillet's end face at y = 15 makes a small step with its own short corners too)
        corners = [h for h in rec["hot_spots"] if h["type"] == ff.SHARP_CORNER
                   and h["length_mm"] == pytest.approx(15.0, rel=0.01)]
        assert [(round(h["angle_deg"]), h["capped"]) for h in corners] == [(90, True)]
        assert corners[0]["at_mm"] == [pytest.approx(6.0, abs=1e-3), pytest.approx(7.5, abs=0.5),
                                       pytest.approx(6.0, abs=1e-3)]
        assert any(h["type"] == "fillet" and h["radius_mm"] == pytest.approx(4.0, rel=STL_RADIUS)
                   for h in rec["hot_spots"])


def test_a_ray_that_grazes_a_neighbouring_face_reads_no_wall():
    # a quarter ring 50-100 mm, 20 mm high: rays from beside its cut planes run along them, and once
    # read a 0.26 mm "wall" on the outer face (FEA step 7's Lame case stopped as over budget)
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common

    ring = fx._cut(fx._cyl(0, 0, 0, 100, 20), fx._cyl(0, 0, -1, 50, 22))
    quarter = BRepAlgoAPI_Common(ring, fx._box(0, 0, -1, 101, 101, 22)).Shape()
    rec = measure_cad(fx._unify(quarter), mm_per_unit=1.0)
    # the true chords near the outer edge are 17-20 mm; a grazing hit read 0.26
    assert rec["thickness"]["min_mm"] >= 15.0


# ------------------------------------------------------- walls that are not walls (21 Oct) ----
# Areen's F1 car (6 bodies, 620 mm) read a 0.00064 mm "thinnest wall": a pin's face 0.0006 mm off
# its hole's, through the tessellation's wobble. Then a strip beside a round read 0.12 mm across a
# taper. Neither is a wall; neither may drive a plan.
def test_two_touching_bodies_are_not_a_wall_between_them():
    rec = measure_cad(fx.pinned_plate(), mm_per_unit=1.0)
    assert rec["bodies"] == 2
    # the hole's wall reads the plate round it, not the pin a hair away
    assert rec["thickness"]["min_mm"] == pytest.approx(6.0, rel=CAD_WALL)
    assert all(f["thickness_min_mm"] >= 6.0 * (1 - CAD_WALL) for f in rec["faces"].values()
               if f.get("thickness_min_mm") is not None)


def test_a_taper_is_not_a_wall():
    rec = measure_cad(fx.beveled_plate(), mm_per_unit=1.0)
    assert rec["thickness"]["min_mm"] == pytest.approx(fx.BEVEL["thickness"], rel=CAD_WALL)


def test_a_head_on_reading_across_a_split_round_still_counts():
    # a rod whose round face OpenCASCADE splits in two halves: a ray from one half meets the other
    # head on, across the rod - its diameter is a wall, even though the halves share an edge
    from tests.fea_fixtures import _cyl

    rec = measure_cad(_cyl(0, 0, 0, 5, 80, axis=(1, 0, 0)), mm_per_unit=1.0)
    assert rec["thickness"]["min_mm"] == pytest.approx(10.0, rel=0.01)


@pytest.mark.parametrize(("readings", "wall"), [
    ([0.03, 10.0, 10.2, 10.5, 11.0], 10.0),     # one stray ray does not set the face's wall
    ([40.0, 40.3, 40.4, 52.0], 40.0),           # the true thinnest reading, not one above it
    ([0.5, 0.55, 0.6, 9.0], 0.5),               # three readings that agree are a thin wall
    ([0.1, 0.1], None),                         # too few rays read anything: no wall of its own
    ([1.0, 2.0, 4.0, 8.0], None),               # readings that scatter agree on nothing
])
def test_a_faces_wall_needs_its_rays_to_agree(readings, wall):
    got = ff.agreed_wall(sorted(readings))
    assert (got if got is None else readings[got]) == (wall if wall is None else pytest.approx(wall))
