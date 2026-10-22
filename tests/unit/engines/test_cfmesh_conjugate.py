"""The conjugate path's guards, pinned against the failures that produced them.

Every assertion here corresponds to something that actually went wrong while the recipe was being
worked out on real geometry, so a future edit that reintroduces one of them fails a test instead of
costing an hour of meshing.
"""
import struct

import pytest

from meshpipeline.engines.cfmesh import conjugate as C


def _tri_normals(stl: bytes):
    n = struct.unpack_from("<I", stl, 80)[0]
    for i in range(n):
        o = 84 + i * 50
        nx, ny, nz = struct.unpack_from("<3f", stl, o)
        verts = [struct.unpack_from("<3f", stl, o + 12 + k * 12) for k in range(3)]
        yield (nx, ny, nz), verts


def test_the_block_is_twelve_triangles_wound_outward():
    # A box with one face wound inward still looks like a box and still tessellates, and cfMesh
    # then meshes the complement of what was intended.
    stl = C.block_surface_stl((0, 0, 0), (2, 3, 4))
    tris = list(_tri_normals(stl))
    assert len(tris) == 12
    centre = (1.0, 1.5, 2.0)
    for n, verts in tris:
        face_mid = tuple(sum(v[k] for v in verts) / 3 for k in range(3))
        outward = tuple(face_mid[k] - centre[k] for k in range(3))
        assert sum(n[k] * outward[k] for k in range(3)) > 0, "a face points inward"


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
    r = C.read_region(text, "fluid")
    assert r.cells == 6088362
    assert r.volume_m3 == pytest.approx(6.3196e-09)
    assert r.pieces == 15
    assert r.max_non_ortho == pytest.approx(14.118)
    assert r.mesh_ok is True          # checkMesh says OK on a mesh that is 15 islands


def test_interface_faces_are_read_per_side():
    boundary = """
    2
    (
        zone0 { type wall; nFaces 17758; startFace 185929; }
        solid_to_fluid { type mappedWall; nFaces 10752; startFace 203687; }
    )
    """
    assert C.interface_faces(boundary, C.SOLID_TO_FLUID) == 10752
    assert C.interface_faces(boundary, "zone0") == 17758
    assert C.interface_faces(boundary, "not_a_patch") == 0


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
