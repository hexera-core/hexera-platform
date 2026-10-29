"""A plate fed and drained through its lid gets named ports, not a refusal.

WHY THIS EXISTS. The port CUTTER was built for exactly this shape and could not be reached from
it. `render_port_patch_dicts` cuts a patch out of a block face so two ports can share one, which
is what a cold plate fed and drained through its lid needs - and the only way into it was through
`stubs_for`, which returns a stub for a mouth that stops INSIDE the metal. A mouth already ON its
face needs no stub, so it got none, so it got no port row, so no patch was cut.

MEASURED END TO END, 2026-10-29, driving the console with nobody steering it. The ported two-scale
plate (two 1.2 mm bores through the lid) reached the conjugate driver and came back with:

    conjugate: meshing without named ports - the coolant reaches no opposite pair of block faces,
    so there is no obvious way for it to flow from one side of the part to the other. It reaches
    zmax. Name a pair yourself with --inlet/--outlet, or cut ports into the coolant geometry ...

Every clause of that is true and the part is still unsolvable: a mesh with no inlet and no outlet
has nothing to apply a flow to, and "cut ports into the coolant geometry" is advice to go and edit
the CAD when the engine can cut them itself.

WHAT MUST NOT CHANGE, and this is the whole care in the fix. Eight of the nine parts in the
package take their ports from RENAMED block faces, proven: a channel milled across a plate ends
flush with the side it was cut on, one mouth per side, an opposite pair. Those must keep the
renamed-face path exactly. So a flush mouth is cut only when the faces reached hold NO opposite
pair - which is precisely the case `plan_ports` refuses.
"""
import pytest

from meshpipeline.engines.cfmesh_conjugate import mouths as MO
from meshpipeline.engines.cfmesh_conjugate.conjugate import render_port_patch_dicts

BLOCK_LO = [0.0, 0.0, 0.0]
BLOCK_HI = [0.024, 0.016, 0.003]


def _mouth(centre, normal=(0.0, 0.0, 1.0), area=1.44e-6, clear=True, passage=0):
    """A row as cad/fluid_volume.write_surface writes it."""
    return {"passage": passage, "centroid_m": list(centre), "normal": list(normal),
            "area_m2": area, "diameter_m": 0.00135, "clear_ahead": clear}


#: THE CUSTOMER'S SHAPE: both bores leave through the lid, so both mouths are flush with zmax.
LID_PAIR = [_mouth((0.018, 0.01395, 0.003)), _mouth((0.006, 0.00205, 0.003))]
#: A CORPUS PART: one mouth on each of two opposite sides, which the renamed-face path handles.
OPPOSITE_PAIR = [_mouth((0.0, 0.008, 0.0015), normal=(-1.0, 0.0, 0.0)),
                 _mouth((0.024, 0.008, 0.0015), normal=(1.0, 0.0, 0.0))]


#   -- which faces can be named at all ---------------------------------------------------------

@pytest.mark.parametrize("faces, ok", [
    (["xmin", "xmax"], True),
    (["ymin", "ymax"], True),
    (["zmin", "zmax"], True),
    (["zmax", "zmax"], False),            # a plate fed and drained through its lid
    (["zmax"], False),
    (["zmax", "xmax"], False),            # one on the lid and one on a side: still no pair
    ([], False),
])
def test_an_opposite_pair_is_the_whole_test(faces, ok):
    """`plan_ports`' rule, written where this module can use it: "a way for the coolant to flow
    from one side of the part to the other" means a pair of faces that face each other."""
    assert MO.nameable_by_faces(faces) is ok


#   -- the lid pair ------------------------------------------------------------------------------

def test_both_bores_through_the_lid_become_cut_ports():
    stubs, said = MO.stubs_for(LID_PAIR, BLOCK_LO, BLOCK_HI)
    assert len(stubs) == 2
    assert [s.face for s in stubs] == ["zmax", "zmax"]
    assert all(s.flush for s in stubs)
    assert any("no opposite pair" in line for line in said)


def test_they_are_named_apart_and_cut_from_the_one_face():
    stubs, _ = MO.stubs_for(LID_PAIR, BLOCK_LO, BLOCK_HI)
    rows, said = MO.port_rows(stubs)
    assert [r["name"] for r in rows] == ["coolant_in", "coolant_out"]
    assert [r["type"] for r in rows] == ["inlet", "outlet"]
    assert [r["face"] for r in rows] == ["zmax", "zmax"]
    topo, patch = render_port_patch_dicts(rows)
    assert topo.count("patch zmax;") == 2, "both ports select from the same block face"
    assert "name coolant_in;" in patch and "name coolant_out;" in patch


def test_the_box_is_cut_where_the_mouth_IS():
    """`crossing` pulls back from `reach` by the overshoot, because a real stub is drawn PAST the
    face on purpose. A flush mouth was never extended, so pulling back would centre the box a
    radius INSIDE the mesh and the cut would miss the face it is meant to narrow."""
    stubs, _ = MO.stubs_for(LID_PAIR, BLOCK_LO, BLOCK_HI)
    for stub, mouth in zip(stubs, LID_PAIR):
        assert stub.crossing == tuple(mouth["centroid_m"])
        assert stub.crossing[2] == pytest.approx(BLOCK_HI[2], abs=1e-12)


def test_a_flush_mouth_changes_the_coolant_not_at_all():
    """It is already on the face. A zero-length tube joined onto the coolant would be a ring of
    degenerate triangles in a surface the mesher reads with surfaceToCell."""
    stubs, _ = MO.stubs_for(LID_PAIR, BLOCK_LO, BLOCK_HI)
    assert all(MO.stub_triangles(s) == [] for s in stubs)
    assert all(s.volume == 0.0 for s in stubs)
    one = [((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0))]
    grown, added = MO.extend(one, stubs)
    assert len(grown) == len(one)
    assert added == 0.0, "nothing was added, so the split's volume check does not move"


#   -- what must not change ----------------------------------------------------------------------

def test_a_part_whose_faces_can_be_renamed_is_left_on_that_path():
    """EIGHT OF THE NINE PARTS IN THE PACKAGE. Cutting their ports instead of renaming their
    faces would replace a proven path with an untried one on every one of them."""
    stubs, said = MO.stubs_for(OPPOSITE_PAIR, BLOCK_LO, BLOCK_HI)
    assert stubs == []
    assert any("can be renamed" in line for line in said)


def test_one_flush_mouth_is_not_cut_at_all():
    """TWO OF THEM, OR NONE, and this rule came from an existing row rather than from thought:
    test_a_mouth_inside_the_block_gets_a_way_out's
    `test_a_mouth_already_flush_with_a_face_is_left_alone` failed on the first version of this
    change, which cut a lone flush mouth out of xmax. It buys nothing - `port_rows` says "a solve
    needs two", and an inlet with no outlet is as unsolvable cut as it is uncut - and a cut patch
    is work and risk on a mesh that will not be solved either way.
    """
    stubs, said = MO.stubs_for([_mouth((0.024, 0.008, 0.0015), normal=(1.0, 0.0, 0.0))],
                               BLOCK_LO, BLOCK_HI)
    assert stubs == []
    assert any("one mouth is not a flow path" in line for line in said)


def test_two_is_the_bar(monkeypatch):
    """The bar is BOTH_ENDS, named in the module rather than written into the condition."""
    assert MO.BOTH_ENDS == 2
    two = [_mouth((0.018, 0.01395, 0.003)), _mouth((0.006, 0.00205, 0.003))]
    assert len(MO.stubs_for(two, BLOCK_LO, BLOCK_HI)[0]) == 2
    assert MO.stubs_for(two[:1], BLOCK_LO, BLOCK_HI)[0] == []


def test_a_bore_that_stops_inside_the_metal_still_gets_a_real_stub():
    stubs, _ = MO.stubs_for([_mouth((0.010, 0.008, 0.0015), normal=(1.0, 0.0, 0.0))],
                            BLOCK_LO, BLOCK_HI)
    assert len(stubs) == 1
    assert not stubs[0].flush
    assert stubs[0].face == "xmax"
    assert stubs[0].length > 0.014, "it has to reach xmax 14 mm away and cross it"
    assert stubs[0].volume > 0.0
    assert len(MO.stub_triangles(stubs[0])) > 0


def test_a_mouth_that_opens_into_another_part_is_still_left_sealed():
    """A stub there would be a passage bored through metal that is not in the customer's CAD, and
    one of the three mouths on the real plate is exactly this."""
    stubs, said = MO.stubs_for([_mouth((0.018, 0.01395, 0.003), clear=False)],
                               BLOCK_LO, BLOCK_HI)
    assert stubs == []
    assert any("opens into another part" in line for line in said)


def test_a_mouth_with_no_flow_area_is_not_a_port():
    stubs, said = MO.stubs_for([_mouth((0.018, 0.01395, 0.003), area=0.0)], BLOCK_LO, BLOCK_HI)
    assert stubs == []
    assert any("no flow area" in line for line in said)


def test_a_real_stub_and_a_flush_one_can_be_in_the_same_part():
    """A plate with one fitting bore and one channel ending flush on a side it cannot be paired
    with: the bore is extended, the flush mouth is cut, and both are named."""
    stubs, said = MO.stubs_for(
        [_mouth((0.010, 0.008, 0.0015), normal=(1.0, 0.0, 0.0)),   # stops inside -> stub
         _mouth((0.018, 0.01395, 0.003))],                          # flush on zmax -> cut
        BLOCK_LO, BLOCK_HI)
    assert len(stubs) == 2
    assert sorted(s.flush for s in stubs) == [False, True]
    assert {s.face for s in stubs} == {"xmax", "zmax"}
    rows, _ = MO.port_rows(stubs)
    assert len(rows) == 2
    topo, _patch = render_port_patch_dicts(rows)
    assert "patch xmax;" in topo and "patch zmax;" in topo


def test_a_stub_that_pairs_with_a_flush_mouth_leaves_it_alone():
    """The stub lands on xmax and the flush mouth is on xmin: that IS an opposite pair, so the
    flush one keeps the block's own name and only the stubbed one is drawn."""
    stubs, said = MO.stubs_for(
        [_mouth((0.010, 0.008, 0.0015), normal=(1.0, 0.0, 0.0)),    # -> xmax, stubbed
         _mouth((0.0, 0.008, 0.0015), normal=(-1.0, 0.0, 0.0))],    # flush on xmin
        BLOCK_LO, BLOCK_HI)
    assert len(stubs) == 1
    assert stubs[0].face == "xmax" and not stubs[0].flush
    assert any("can be renamed" in line for line in said)
