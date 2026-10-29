"""A port is cut out of a block face, so two of them can land on the same one.

WHY THIS EXISTS. `renameBoundary` gives a whole block face the user's name. That works while one
port lands on each face and fails the moment two do, because a face can only carry one name. The
first real customer assembly is exactly that: its inlet and outlet are both fittings pointing +x,
so after their bores are stubbed out to the boundary BOTH land on `xmax`, and `plan_ports` then
refuses the part for "reaching no opposite pair of block faces" - a true statement about the
block and nothing about the plumbing. A cold plate fed and drained through its lid has the same
shape, and so does `murray_ported`, which drains through sixteen holes in one face.

PROVEN ON A REAL MESH, 2026-10-29, on the ported two-scale plate whose two bores both leave
through the lid:

    fluid   fluid_to_solid 27,250   coolant_in 36   coolant_out 36
    solid   xmin 1,312  xmax 580  ymin 1,298  ymax 1,298  zmin 9,968  zmax 9,896  solid_to_fluid 27,250

Every one of the coolant's 72 faces on zmax became a port, and the metal's zmax is untouched.

CUT AFTER THE SPLIT, ON THE COOLANT ALONE, and that placement was measured rather than reasoned.
Cut on the block mesh beforehand - one cut serving both regions, as a renamed face genuinely does
- the box round a mouth catches the lid metal ringing the bore too, and the metal came back with
50 and 53 faces of external wall called `coolant_in` and `coolant_out`. That is how a solver ends
up with a mass-flow inlet on the outside of a plate.
"""
import pytest

from meshpipeline.engines.cfmesh_conjugate.conjugate import (
    PORT_BOX_DIAMETERS,
    ConjugateError,
    render_port_patch_dicts,
)


def _port(**kw):
    p = {"name": "coolant_in", "type": "inlet", "face": "xmax",
         "centre_m": (0.1159, 0.040, 0.030), "diameter_m": 0.0032}
    p.update(kw)
    return p


def test_a_part_with_no_cut_ports_gets_no_dictionaries():
    """Most parts take their ports from renamed faces and must be untouched by this."""
    assert render_port_patch_dicts([]) == ("", "")


def test_two_ports_on_one_face_is_the_whole_point():
    topo, patch = render_port_patch_dicts([
        _port(name="coolant_in", type="inlet", face="xmax"),
        _port(name="coolant_out", type="outlet", face="xmax",
              centre_m=(0.1159, 0.040, 0.060)),
    ])
    assert topo.count("patch xmax;") == 2, "both ports select from the same block face"
    assert "name coolant_in;" in patch and "name coolant_out;" in patch
    assert "type patch;" in patch


def test_the_face_is_narrowed_by_a_box_round_the_mouth():
    """patchToFace takes the whole face; the box is what makes it a port rather than a wall."""
    topo, _ = render_port_patch_dicts([_port(centre_m=(1.0, 2.0, 3.0), diameter_m=0.004)])
    r = PORT_BOX_DIAMETERS * 0.004
    assert "source patchToFace" in topo and "source boxToFace" in topo
    assert f"box ({1.0 - r:.9g} {2.0 - r:.9g} {3.0 - r:.9g})" in topo
    assert topo.index("patchToFace") < topo.index("boxToFace"), (
        "the whole face has to be selected before it is narrowed")
    assert "action new" in topo and "action subset" in topo


def test_the_role_becomes_the_openfoam_type():
    _, inlet = render_port_patch_dicts([_port(type="inlet")])
    _, wall = render_port_patch_dicts([_port(type="wall")])
    assert "type patch;" in inlet
    assert "type wall;" in wall


def test_every_entry_is_set_based():
    """snappy_multiregion records what happens otherwise: "a whole-patch move in the same dict as
    the set moves overrides them (the ports came back empty and every exterior face became
    wall)". What is left of the block face keeps its name by nothing being done to it."""
    _, patch = render_port_patch_dicts([_port(), _port(name="coolant_out",
                                                       centre_m=(0.1159, 0.04, 0.06))])
    assert patch.count("constructFrom set;") == 2
    assert "constructFrom patches" not in patch
    assert "pointSync false;" in patch


@pytest.mark.parametrize("bad, why", [
    ({"name": ""}, "no name"),
    ({"face": "middle"}, "not a block face"),
    ({"face": ""}, "no face"),
    ({"diameter_m": 0.0}, "no diameter"),
    ({"diameter_m": -1.0}, "a negative diameter"),
    ({"centre_m": (0.1, 0.2)}, "a centre in two axes"),
])
def test_a_port_that_cannot_be_cut_is_refused(bad, why):
    with pytest.raises(ConjugateError):
        render_port_patch_dicts([_port(**bad)])


def test_two_ports_with_the_same_name_are_refused():
    """They would produce one patch and the second would silently replace the first."""
    with pytest.raises(ConjugateError, match="both called"):
        render_port_patch_dicts([_port(), _port(centre_m=(0.1159, 0.04, 0.06))])


def test_the_selection_sets_are_named_apart_from_the_patches():
    """A faceSet and a patch sharing a name is how createPatch comes to read the wrong one."""
    topo, patch = render_port_patch_dicts([_port(name="coolant_in")])
    assert "name port_coolant_in;" in topo
    assert "set port_coolant_in;" in patch
    assert "name coolant_in;" in patch


#   -- from a measured mouth to a port row ---------------------------------------------------

def _mouth(**kw):
    """A row as cad/fluid_volume.write_surface writes it."""
    p = {"passage": 0, "centroid_m": [0.1100, 0.040, 0.030], "normal": [1.0, 0.0, 0.0],
         "area_m2": 8.04e-6, "diameter_m": 0.0032, "clear_ahead": True}
    p.update(kw)
    return p


BLOCK_LO = [0.0, 0.0, 0.0]
BLOCK_HI = [0.1159, 0.0619, 0.0924]


def test_a_bore_that_stops_inside_the_metal_gets_a_stub_to_the_face():
    from meshpipeline.engines.cfmesh_conjugate import mouths as MO

    stubs, said = MO.stubs_for([_mouth()], BLOCK_LO, BLOCK_HI)
    assert len(stubs) == 1
    assert stubs[0].face == "xmax"
    assert stubs[0].length > 0.0059, "it has to reach the face 5.9 mm away and cross it"


def test_the_patch_is_cut_where_the_stub_crosses_not_where_it_ends():
    """`reach` is deliberately PAST the face so the surface crosses rather than lands on it. A
    box centred there sits outside the mesh and catches the face only by its own half-width."""
    from meshpipeline.engines.cfmesh_conjugate import mouths as MO

    stubs, _ = MO.stubs_for([_mouth()], BLOCK_LO, BLOCK_HI)
    s = stubs[0]
    assert s.crossing[0] == pytest.approx(BLOCK_HI[0], abs=1e-9)
    assert s.reach[0] > s.crossing[0], "reach overshoots on purpose"


def test_two_bores_pointing_the_same_way_become_two_ports_on_one_face():
    """THE CUSTOMER'S SHAPE. Both fittings point +x, so both stubs land on xmax - which is the
    case `plan_ports` refuses for "reaching no opposite pair of block faces"."""
    from meshpipeline.engines.cfmesh_conjugate import mouths as MO

    stubs, _ = MO.stubs_for(
        [_mouth(centroid_m=[0.1100, 0.040, 0.030]),
         _mouth(centroid_m=[0.1100, 0.040, 0.060])], BLOCK_LO, BLOCK_HI)
    rows, said = MO.port_rows(stubs)
    assert [r["face"] for r in rows] == ["xmax", "xmax"]
    assert [r["type"] for r in rows] == ["inlet", "outlet"]
    assert [r["name"] for r in rows] == ["coolant_in", "coolant_out"]
    assert "Both are on one face" in said
    assert "guess" in said, "which end is fed is not something the geometry says"
    # and those rows are exactly what the cutter takes
    topo, patch = render_port_patch_dicts(rows)
    assert topo.count("patch xmax;") == 2


def test_a_mouth_that_opens_into_another_part_is_left_sealed():
    """A stub there would be a passage bored through metal that is not in the customer's CAD.
    One of the three mouths on the real plate is exactly this."""
    from meshpipeline.engines.cfmesh_conjugate import mouths as MO

    stubs, said = MO.stubs_for([_mouth(clear_ahead=False)], BLOCK_LO, BLOCK_HI)
    assert stubs == []
    assert any("opens into another part" in s for s in said)


def test_one_mouth_is_not_a_flow_path_and_says_so():
    from meshpipeline.engines.cfmesh_conjugate import mouths as MO

    stubs, _ = MO.stubs_for([_mouth()], BLOCK_LO, BLOCK_HI)
    rows, said = MO.port_rows(stubs)
    assert len(rows) == 1
    assert "a solve needs two" in said


def test_the_driver_grows_the_coolant_and_cuts_the_ports():
    """Both halves have to happen. A stub that is not joined to the surface leaves the coolant
    stopping inside the metal; a patch that is not cut leaves it with no name to apply a flow to."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate import driver as D

    src = inspect.getsource(D.drive)
    assert "stubs_for" in src, "the driver has to work out which mouths can be opened"
    assert "write_stl_binary" in src, "and join the stubs onto the coolant it meshes"
    assert "port_rows" in src, "and turn them into patches to cut"
    assert "port_patches=tuple(port_patches)" in src, "and put them in the plan"


def test_the_runner_actually_cuts_them():
    """THE GAP THIS PACKAGE HAS HAD TWICE. A renderer tested through its own front door cannot
    catch a caller that never calls it - `render_meshdict` took fine_zones for months while
    `build` passed none, and a 78-minute run rebuilt an identical mesh and exited 0."""
    import inspect

    from meshpipeline.engines.cfmesh_conjugate.conjugate import build

    assert "port_patches" in inspect.signature(build).parameters
    src = inspect.getsource(build)
    assert "render_port_patch_dicts" in src, "build has to render the dictionaries"
    assert "createPatch -overwrite -region" in src, "and run createPatch on the coolant"
    # after the split, not before it: the order is the finding, not an implementation detail
    assert src.index("splitMeshRegions") < src.index("createPatch -overwrite"), (
        "cutting before the split gives the metal patches named after the coolant's ports")
