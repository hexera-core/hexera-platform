"""The delivered mesh carries the names the customer gave the ports.

THE THING MOST LIKELY TO HAVE STOPPED THE FIRST END-TO-END RUN. The mesher hands cfMesh the block
as six named solids - xmin, xmax, ymin, ymax, zmin, zmax - and wrote no renameBoundary at all. So
a customer who declared an opening called coolant_in got a delivered mesh with no patch of that
name, and the patch-contract gate refuses a job whose declared patches are not in the mesh. The
mesh would have been geometrically perfect and refused anyway.

MEASURED IN THE MESH IMAGE on unit_cell at 4 cells across, renaming ymin to coolant_in and ymax to
coolant_out:

    fluid   coolant_in 128   coolant_out 128   fluid_to_solid 93,600
    solid   coolant_in 404   coolant_out 404   xmin 67,604   xmax 67,604
            zmin 3,900   zmax 13,000   solid_to_fluid 93,600
    root    coolant_in 532   coolant_out 532   xmin 67,604 ...   (pre-split, for contrast)

    problems: []

Three things that answers. The fluid's port carries the user's name with the same 128 faces ymin
had, so nothing was lost in the rename. The five faces NOT named keep their own names, so a rename
does not swallow the rest - which it would have with a defaultName, as it did on the 2D cfMesh
path where a default absorbed 34,000 faces that identity entries had failed to match. And cfMesh
renames BEFORE the split, so both regions inherit the name: the coolant's end at 128 faces and the
metal's end face at 404 are two different faces of the same plane, each in its own region, and
OpenFOAM patch names are per-region so that is what should happen.
"""
from __future__ import annotations

import pytest

from meshpipeline.engines.cfmesh_conjugate import conjugate as C


# #
# what the dictionary says
# #
def test_no_names_means_no_rename_section():
    """The path every existing run takes, unchanged: without declared names the block keeps its
    six face names and the dictionary has no renameBoundary at all."""
    out = C.render_meshdict(block_file="b.stl", fluid_file="f.stl", background_cell_m=0.0002)
    assert "renameBoundary" not in out


def test_a_named_port_is_renamed_with_its_openfoam_type():
    out = C.render_meshdict(block_file="b.stl", fluid_file="f.stl", background_cell_m=0.0002,
                            patch_names={"ymin": ("coolant_in", "inlet"),
                                         "ymax": ("coolant_out", "outlet")})
    assert "renameBoundary" in out
    assert "ymin { newName coolant_in; type patch; }" in out
    assert "ymax { newName coolant_out; type patch; }" in out


def test_the_faces_nobody_named_are_not_mentioned():
    """So they keep their own names. A defaultName would swallow them - on the 2D cfMesh path one
    absorbed 34,000 faces that identity entries had failed to match."""
    out = C.render_meshdict(block_file="b.stl", fluid_file="f.stl", background_cell_m=0.0002,
                            patch_names={"ymin": ("coolant_in", "inlet")})
    assert "defaultName" not in out
    assert "defaultType" not in out
    for face in ("xmin", "xmax", "zmin", "zmax", "ymax"):
        assert f"{face} {{" not in out, f"{face} was not named and should not be renamed"


@pytest.mark.parametrize(("role", "of_type"), [
    ("inlet", "patch"), ("outlet", "patch"), ("wall", "wall"),
    ("external", "wall"), ("symmetry", "symmetryPlane"), ("empty", "empty"),
])
def test_each_declared_role_becomes_its_openfoam_type(role, of_type):
    out = C.render_meshdict(block_file="b.stl", fluid_file="f.stl", background_cell_m=0.0002,
                            patch_names={"zmax": ("lid", role)})
    assert f"zmax {{ newName lid; type {of_type}; }}" in out


def test_a_role_nobody_recognises_is_a_plain_patch():
    out = C.render_meshdict(block_file="b.stl", fluid_file="f.stl", background_cell_m=0.0002,
                            patch_names={"zmax": ("lid", "something_new")})
    assert "zmax { newName lid; type patch; }" in out


def test_a_name_for_a_face_the_block_does_not_have_is_ignored():
    # The block has exactly six faces; anything else is a caller mistake and must not reach the
    # dictionary, where cfMesh would have no entry to match it.
    out = C.render_meshdict(block_file="b.stl", fluid_file="f.stl", background_cell_m=0.0002,
                            patch_names={"not_a_face": ("nowhere", "inlet")})
    assert "renameBoundary" not in out
    assert "nowhere" not in out


# #
# ...and the gate reads the name the mesh carries
# #
def _report(fluid_patches: dict, names: dict) -> C.ConjugateReport:
    fluid = C.RegionReport(name=C.FLUID_REGION, cells=100, volume_m3=2.6e-9, pieces=1,
                           mesh_ok=True)
    solid = C.RegionReport(name=C.SOLID_REGION, cells=100, volume_m3=5.9e-9, pieces=1,
                           mesh_ok=True)
    fluid.patches = dict(fluid_patches)
    solid.patches = {C.SOLID_TO_FLUID: 10}
    rep = C.ConjugateReport(fluid=fluid, solid=solid,
                            ports=C.PortPlan(inlet="ymin", outlet="ymax", walls=(), reason="t"),
                            patch_names=names)
    rep.interface_faces_fluid = rep.interface_faces_solid = 10
    fluid.patches.setdefault(C.FLUID_TO_SOLID, 10)
    return rep


def test_the_gate_looks_for_the_declared_name_not_the_block_face():
    """Measured before this: the mesh had coolant_in with 128 faces and the gate reported "the
    inlet was to be ymin, and the fluid region has no patch of that name"."""
    rep = _report({"coolant_in": 128, "coolant_out": 128},
                  {"ymin": "coolant_in", "ymax": "coolant_out"})
    out = C.verify(rep, expect_fluid_volume_m3=None, expect_fluid_pieces=None)
    assert out.problems == [], out.problems


def test_a_missing_port_is_still_caught_under_its_declared_name():
    rep = _report({"coolant_out": 128}, {"ymin": "coolant_in", "ymax": "coolant_out"})
    out = C.verify(rep, expect_fluid_volume_m3=None, expect_fluid_pieces=None)
    assert any("coolant_in" in q and "no patch of that name" in q for q in out.problems), out.problems
    assert any("the ymin face, renamed" in q for q in out.problems), (
        "the message should say which face it was cut from")


def test_a_declared_port_with_zero_faces_is_still_the_failure_that_matters():
    rep = _report({"coolant_in": 0, "coolant_out": 128},
                  {"ymin": "coolant_in", "ymax": "coolant_out"})
    out = C.verify(rep, expect_fluid_volume_m3=None, expect_fluid_pieces=None)
    assert any("ZERO faces" in q and "coolant_in" in q for q in out.problems), out.problems


def test_an_unrenamed_port_is_checked_by_its_face_name_as_before():
    rep = _report({"ymin": 128, "ymax": 128}, {})
    out = C.verify(rep, expect_fluid_volume_m3=None, expect_fluid_pieces=None)
    assert out.problems == [], out.problems
