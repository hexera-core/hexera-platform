"""The conjugate path's guards, pinned against the failures that produced them.

Every assertion here corresponds to something that actually went wrong while the recipe was being
worked out on real geometry, so a future edit that reintroduces one of them fails a test instead of
costing an hour of meshing.
"""
import pytest

from meshpipeline.engines.cfmesh_conjugate import conjugate as C
from meshpipeline.engines.cfmesh.foam_exec import parse_check_mesh


def _ascii_tris(stl: str):
    """(solid name, normal, three vertices) for every facet of an ASCII STL.

    The block surface became ASCII with one named solid per face, because cfMesh takes its patch
    names from those names and a binary STL carries none. A nameless box cannot have an inlet.
    """
    name = None
    normal = None
    verts = []
    for line in stl.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "solid":
            name = parts[1]
        elif parts[0] == "facet":
            normal = tuple(float(v) for v in parts[2:5])
            verts = []
        elif parts[0] == "vertex":
            verts.append(tuple(float(v) for v in parts[1:4]))
        elif parts[0] == "endfacet":
            yield name, normal, verts


def test_the_block_is_twelve_triangles_wound_outward():
    # A box with one face wound inward still looks like a box and still tessellates, and cfMesh
    # then meshes the complement of what was intended.
    tris = list(_ascii_tris(C.block_surface_stl((0, 0, 0), (2, 3, 4))))
    assert len(tris) == 12
    centre = (1.0, 1.5, 2.0)
    for _name, n, verts in tris:
        face_mid = tuple(sum(v[k] for v in verts) / 3 for k in range(3))
        outward = tuple(face_mid[k] - centre[k] for k in range(3))
        assert sum(n[k] * outward[k] for k in range(3)) > 0, "a face points inward"


def test_every_block_face_is_its_own_named_solid():
    # cfMesh names a patch after the STL solid it came from, and splitMeshRegions carries those
    # patches into both regions. That is the whole mechanism by which the coolant gets an inlet:
    # one anonymous box gives one patch covering every outer face, and then the faces the coolant
    # reaches and the faces the metal reaches cannot be told apart.
    tris = list(_ascii_tris(C.block_surface_stl((0, 0, 0), (2, 3, 4))))
    by_name = {}
    for name, _n, _v in tris:
        by_name[name] = by_name.get(name, 0) + 1
    assert set(by_name) == set(C.BLOCK_FACES)
    assert set(by_name.values()) == {2}, "each face is two triangles"


def test_a_named_face_can_be_grown_to_put_a_lid_over_the_coolant():
    # A part whose channels are open at the top has its coolant flush with the block's own zmax.
    # Growing zmax puts metal over them, so the channel roof stops being an outer boundary and
    # becomes part of the interface, where heat can cross it.
    lo, hi = C.block_bounds((0, 0, 0), (2, 3, 4), {"zmax": 0.5})
    assert (lo, hi) == ((0, 0, 0), (2, 3, 4.5))
    lo, hi = C.block_bounds((0, 0, 0), (2, 3, 4), {"ymin": 0.25})
    assert (lo, hi) == ((0, -0.25, 0), (2, 3, 4))


def test_a_face_cannot_be_grown_inward_and_an_unknown_face_is_refused():
    with pytest.raises(C.ConjugateError):
        C.block_bounds((0, 0, 0), (2, 3, 4), {"zmax": -0.5})
    with pytest.raises(C.ConjugateError):
        C.block_bounds((0, 0, 0), (2, 3, 4), {"top": 0.5})


def test_a_degenerate_block_is_refused():
    with pytest.raises(C.ConjugateError):
        C.block_surface_stl((0, 0, 0), (0, 3, 4))


def test_the_meshdict_refines_on_the_fluid_without_meshing_it():
    # The coolant surface must appear ONLY as a refinement source. The moment it becomes the
    # surfaceFile, cfMesh meshes the coolant alone and there is no solid region to pair it with.
    d = C.render_meshdict(block_file="b.stl", fluid_file="f.stl",
                          background_cell_m=80e-6, levels=3)
    assert 'surfaceFile "b.stl";' in d
    assert "surfaceMeshRefinement" in d
    assert 'surfaceFile "f.stl";' in d
    assert d.count("surfaceFile") == 2
    assert "additionalRefinementLevels 3;" in d


def test_the_interface_cell_is_the_background_halved_per_level():
    assert C.interface_cell_m(80e-6, 3) == pytest.approx(10e-6)
    assert C.interface_cell_m(160e-6, 3) == pytest.approx(20e-6)
    assert C.interface_cell_m(40e-6, 3) == pytest.approx(5e-6)


def test_both_zones_are_named():
    # An unzoned remainder leaves splitMeshRegions to decide what those cells are, and what it
    # decides is one region per connected lump of metal.
    d = C.render_toposetdict(fluid_file="f.stl")
    assert f"name      {C.FLUID_REGION};" in d
    assert f"name      {C.SOLID_REGION};" in d
    assert "useSurfaceOrientation true;" in d
    # cellToCell, not setToCell: setToCell does not exist and topoSet rejects the whole
    # dictionary, so the mesh is built and then discarded on a one-word typo.
    assert "source    cellToCell;" in d
    assert "setToCell;" not in d


def test_the_split_uses_zones_only():
    # -cellZones walks face connectivity first and produced 84 regions on a 100-channel part.
    assert "-cellZonesOnly" in C.SPLIT_ARGS
    assert "-cellZones" not in [a for a in C.SPLIT_ARGS if a != "-cellZonesOnly"]


def test_region_parse_reads_what_checkmesh_prints():
    text = """
    Mesh stats
        points:           6671148
        cells:            6088362
   *Number of regions: 15
        Min volume = 8.8e-16. Max volume = 8.6e-15.  Total volume = 6.3196e-09.  Cell volumes OK.
        Mesh non-orthogonality Max: 14.118 average: 0.767893
        Max skewness = 0.286827 OK.
    Mesh OK.
    """
    # Through foam_exec's parser, not a second one. A conjugate-only reader of checkMesh output
    # was a second thing that could disagree with the first about what checkMesh said.
    r = C.read_region(parse_check_mesh(text), "fluid")
    assert r.cells == 6088362
    assert r.volume_m3 == pytest.approx(6.3196e-09)
    assert r.pieces == 15
    assert r.max_non_ortho == pytest.approx(14.118)
    assert r.mesh_ok is True          # checkMesh says OK on a mesh that is 15 islands


def test_interface_faces_are_read_per_side(tmp_path):
    # Read through polymesh_surface, which engines/passage and engines/passage_flow already use.
    polymesh = tmp_path / "polyMesh"
    polymesh.mkdir()
    (polymesh / "boundary").write_text("""FoamFile { format ascii; }
    3
    (
        zone0 { type wall; nFaces 17758; startFace 185929; }
        solid_to_fluid { type mappedWall; nFaces 10752; startFace 203687; }
        zmax { type wall; nFaces 0; startFace 214439; }
    )
    """)
    patches = C.boundary_patches(polymesh)
    assert C.interface_faces(patches, C.SOLID_TO_FLUID) == 10752
    assert C.interface_faces(patches, "zone0") == 17758
    assert C.interface_faces(patches, "not_a_patch") == 0
    # A patch can survive the split with ZERO faces, which is a name with nothing behind it and
    # the exact shape of a port the coolant does not reach.
    assert C.interface_faces(patches, "zmax") == 0


def _report(**kw):
    base = dict(fluid=C.RegionReport("fluid", cells=10, volume_m3=6.3196e-09, pieces=1,
                                     mesh_ok=True),
                solid=C.RegionReport("solid", cells=20, volume_m3=1.3e-07, pieces=1, mesh_ok=True),
                interface_faces_fluid=1065510, interface_faces_solid=1065510)
    base.update(kw)
    return C.ConjugateReport(**base)


def test_a_good_mesh_passes_every_check():
    r = C.verify(_report(), expect_fluid_volume_m3=6.311812e-09, expect_fluid_pieces=1)
    assert r.conformal is True
    assert r.ok is True, r.problems


def test_mismatched_interface_faces_are_refused():
    r = C.verify(_report(interface_faces_solid=1065509))
    assert r.conformal is False
    assert any("not conformal" in p for p in r.problems)


def test_conformal_but_wrongly_tagged_is_still_refused():
    # The point of the volume check. A split that put half the metal in the coolant region would
    # be perfectly conformal and completely wrong.
    r = C.verify(_report(fluid=C.RegionReport("fluid", cells=10, volume_m3=6.0e-08, pieces=1,
                                              mesh_ok=True)),
                 expect_fluid_volume_m3=6.311812e-09)
    assert r.conformal is True
    assert any("not the coolant" in p for p in r.problems)


def test_islands_are_refused_even_though_checkmesh_said_ok():
    r = C.verify(_report(fluid=C.RegionReport("fluid", cells=10, volume_m3=6.3196e-09, pieces=15,
                                              mesh_ok=True)),
                 expect_fluid_pieces=1)
    assert any("disconnected piece" in p for p in r.problems)
    assert any("will not solve" in p for p in r.problems)


def test_a_multi_passage_part_is_allowed_to_be_many_pieces():
    # 100 through-channels that never touch each other are legitimately 100 pieces. The gate is
    # the EXPECTED count, not "one".
    r = C.verify(_report(fluid=C.RegionReport("fluid", cells=10, volume_m3=6.3196e-09, pieces=100,
                                              mesh_ok=True)),
                 expect_fluid_pieces=100)
    assert r.ok is True, r.problems


# #
# ports: a coolant region with no way in or out passes every geometric check and cannot be solved
# #
#: A plate 10 x 20 x 1 mm, with coolant running the length of it and flush with both ends.
_BLOCK = ((0.0, 0.0, 0.0), (0.010, 0.020, 0.001))
_THROUGH = ((0.002, 0.0, 0.0002), (0.008, 0.020, 0.0008))


def test_open_faces_are_the_ones_the_coolant_reaches():
    assert C.open_faces(_THROUGH[0], _THROUGH[1], *_BLOCK) == ("ymin", "ymax")


def test_a_sealed_coolant_region_reaches_nothing():
    sealed = ((0.002, 0.002, 0.0002), (0.008, 0.018, 0.0008))
    assert C.open_faces(sealed[0], sealed[1], *_BLOCK) == ()


def test_a_face_a_hair_short_of_the_block_still_counts_as_reached():
    # The STL is written in float32, so a face that is exactly flush in the CAD lands a few parts
    # in 10^7 off the plane. An absolute nanometre tolerance calls that sealed.
    nearly = ((0.002, 2.0e-9, 0.0002), (0.008, 0.020 - 2.0e-9, 0.0008))
    assert C.open_faces(nearly[0], nearly[1], *_BLOCK) == ("ymin", "ymax")


#: The spans of ONE passage, which is what the flow axis is chosen from.
_PASSAGE = (0.006, 0.020, 0.0006)


def test_the_default_flow_axis_is_the_long_way_down_one_passage():
    plan = C.plan_ports(("xmin", "xmax", "ymin", "ymax"), _PASSAGE)
    assert (plan.inlet, plan.outlet) == ("ymin", "ymax")
    # The sides the block was cut on are walls, and that is not a defect.
    assert plan.walls == ("xmin", "xmax")


def test_the_flow_axis_comes_from_the_passage_and_not_the_whole_coolant():
    # Tuckerman and Pease, 1981: 200 channels, each 14 mm long, spread across 20 mm of silicon.
    # The coolant's own bounding box is 20 mm in x against 14 in y, so ranking axes by it sends
    # the flow ACROSS two hundred dead ends. One channel is 0.05 x 14 x 0.302, and picks y.
    whole_fluid = (0.01995, 0.014, 0.000302)
    one_channel = (0.00005, 0.014, 0.000302)
    faces = ("xmin", "xmax", "ymin", "ymax")
    assert C.plan_ports(faces, whole_fluid).inlet == "xmin"      # the wrong answer, kept as proof
    assert C.plan_ports(faces, one_channel).inlet == "ymin"      # the right one


def test_two_faces_that_are_not_opposite_are_used_and_the_guess_is_declared():
    # A cold plate fed through its side and drained through its lid. Nothing in the geometry says
    # which end is cold, so the direction is a guess and has to say so.
    plan = C.plan_ports(("ymin", "zmax"), _PASSAGE)
    assert (plan.inlet, plan.outlet) == ("ymin", "zmax")
    assert "guess" in plan.reason


def test_a_sealed_part_is_refused_with_the_reason():
    with pytest.raises(C.ConjugateError, match="sealed inside the metal"):
        C.plan_ports((), _PASSAGE)


def test_a_port_the_coolant_does_not_reach_is_refused():
    with pytest.raises(C.ConjugateError, match="does not reach"):
        C.plan_ports(("ymin", "ymax"), _PASSAGE, inlet="zmin", outlet="ymax")


def test_a_named_port_pair_overrides_the_default():
    plan = C.plan_ports(("xmin", "xmax", "ymin", "ymax"), _PASSAGE,
                        inlet="xmin", outlet="xmax")
    assert (plan.inlet, plan.outlet) == ("xmin", "xmax")
    assert plan.walls == ("ymin", "ymax")


def test_an_inlet_patch_with_no_faces_is_a_failure_not_a_warning():
    # This is the one checkMesh is happiest about. Every geometric bar passes on a sealed coolant
    # region and it cannot be solved: nothing fixes its pressure, so the run dies in the
    # preconditioner. The patch NAME survives the split whether or not any coolant reached it.
    ports = C.PortPlan(inlet="ymin", outlet="ymax")
    r = _report(ports=ports)
    r.fluid.patches = {C.FLUID_TO_SOLID: 1065510, "ymin": 0, "ymax": 236}
    C.verify(r, expect_fluid_volume_m3=6.311812e-09, expect_fluid_pieces=1)
    assert r.ok is False
    assert any("ZERO faces" in p for p in r.problems)
    assert r.flowable is False


def test_both_ports_carrying_faces_is_what_makes_a_mesh_flowable():
    ports = C.PortPlan(inlet="ymin", outlet="ymax")
    r = _report(ports=ports)
    r.fluid.patches = {C.FLUID_TO_SOLID: 1065510, "ymin": 236, "ymax": 236}
    C.verify(r, expect_fluid_volume_m3=6.311812e-09, expect_fluid_pieces=1)
    assert r.ok is True, r.problems
    assert r.flowable is True
    assert r.inlet_faces == 236 and r.outlet_faces == 236


def test_a_fatal_checkmesh_defect_is_carried_through_as_a_problem():
    r = _report()
    r.fluid.fatal = ("negative-volume cells",)
    C.verify(r)
    assert any("negative-volume cells" in p for p in r.problems)


def test_cells_across_is_measured_from_the_mesh_and_reported_as_a_lower_bound():
    # 100 um passage, cells averaging 20 um: five across. Evidence about what the mesh achieved,
    # never a second sizing rule - engines/gap_sizing owns that one.
    region = C.RegionReport("fluid", cells=1000, volume_m3=1000 * (20e-6 ** 3))
    assert region.mean_cell_m == pytest.approx(20e-6)
    assert region.cells_across(100e-6) == pytest.approx(5.0)

def test_a_background_cell_too_big_for_the_block_is_reported():
    # cfMesh cannot place a 400 um cell inside a 200 um wide block, so the octree levels asked for
    # are not the ones delivered. Measured on unit_cell_100um at 2 cells across: a 200 um wide
    # block against a 400 um background.
    why = C.background_complaint((0.0, 0.0, 0.0), (0.0002, 0.0325, 0.0013), 400e-6)
    assert "400 um" in why and "200 um" in why
    assert "0.50 background cells" in why
    # ...and it says what it does NOT tell you, because a guess that it would separate accurate
    # meshes from inaccurate ones was wrong: it fired on the worst two parts and the third best.
    assert "says nothing about whether the mesh will be accurate" in why


def test_a_background_cell_the_block_can_hold_is_not_reported():
    # corintis_straight_baseline_100um at 2 across: 1.1 mm thinnest against a 400 um background.
    assert C.background_complaint((0.0, 0.0, 0.0), (0.0199, 0.0325, 0.0011), 400e-6) == ""
    assert C.background_complaint((0.0, 0.0, 0.0), (0.01, 0.01, 0.01), 0.0) == ""
