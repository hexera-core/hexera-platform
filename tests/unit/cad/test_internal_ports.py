# Responsibility: Verify every flat opening of an internal-flow solid becomes its own port.
from __future__ import annotations

import math

import pytest

pytest.importorskip("OCP.STEPControl")

from meshpipeline.cad.cad_tessellate import tessellate_internal  # noqa: E402
from meshpipeline.contracts.coordinate_state import from_occ_transfer  # noqa: E402
from meshpipeline.contracts.geometry_units import (  # noqa: E402
    GeometryInterpretation,
    LengthUnit,
    ResolutionBasis,
)


def _prepared():
    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t",
        unit=LengthUnit.millimetre, scale_to_metres=0.001,
        basis=ResolutionBasis.file_declared, evidence="declared in the file as millimetre")
    return from_occ_transfer(interp, LengthUnit.millimetre)


def _y_manifold(path):
    """A 60 mm run splitting into two 40 mm branches - three flat openings."""
    import gmsh
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("y")
        occ = gmsh.model.occ
        run = occ.addCylinder(0, 0, 0, 250, 0, 0, 30)
        th = math.radians(30)
        a = occ.addCylinder(250, 0, 0, 200 * math.cos(th), 200 * math.sin(th), 0, 20)
        b = occ.addCylinder(250, 0, 0, 200 * math.cos(th), -200 * math.sin(th), 0, 20)
        # a blend ball at the junction, as a real manifold would have: without it the fusion
        # leaves a flat RING of the main run's end cap around the two branch mouths, and that ring
        # is indistinguishable from a port by flatness alone (see the limitation test below)
        blend = occ.addSphere(250, 0, 0, 30)
        occ.fuse([(3, run)], [(3, a), (3, b), (3, blend)])
        occ.synchronize()
        gmsh.write(str(path))
    finally:
        gmsh.finalize()


def _duct(path):
    import gmsh
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("duct")
        gmsh.model.occ.addCylinder(0, 0, 0, 300, 0, 0, 25)
        gmsh.model.occ.synchronize()
        gmsh.write(str(path))
    finally:
        gmsh.finalize()


def test_every_opening_of_a_manifold_becomes_its_own_port(tmp_path):
    # Keeping only the two largest openings and folding the rest into the wall SEALS them, and
    # nothing downstream can tell. A Y-junction came back with one branch capped, meshed 1,659,660
    # cells and reported production-grade - a mesh flow could only leave through one branch of.
    step = tmp_path / "y.step"
    _y_manifold(step)
    out = tmp_path / "stls"
    out.mkdir()

    t = tessellate_internal(str(step), out, prepared=_prepared())

    names = set(t["openings"])
    assert names == {"inlet", "outlet_1", "outlet_2"}, names
    # the feed is the largest port; both branches are open and equal
    areas = {k: v["area"] * 1e6 for k, v in t["openings"].items()}
    assert areas["inlet"] == pytest.approx(math.pi * 30 ** 2, rel=0.02), areas
    assert areas["outlet_1"] == pytest.approx(math.pi * 20 ** 2, rel=0.02), areas
    assert areas["outlet_2"] == pytest.approx(math.pi * 20 ** 2, rel=0.02), areas
    # and each one is written as its own patch, not welded into the wall
    written = {p.stem for p in out.glob("*.stl")}
    assert written == {"wall", "inlet", "outlet_1", "outlet_2"}, written


def test_a_plain_duct_still_names_its_two_ports_inlet_and_outlet(tmp_path):
    # the two-port case keeps its original naming and its axis-order rule - no regression
    step = tmp_path / "duct.step"
    _duct(step)
    out = tmp_path / "stls"
    out.mkdir()

    t = tessellate_internal(str(step), out, prepared=_prepared())

    assert set(t["openings"]) == {"inlet", "outlet"}
    assert {p.stem for p in out.glob("*.stl")} == {"wall", "inlet", "outlet"}


# KNOWN LIMITATION, recorded deliberately rather than left to be rediscovered in a customer's mesh.
#
# Port detection is "the face is planar". That is not a reliable discriminator: a flat face which is
# WALL - the ring left where two branches punch through the end of a run, a panel of a box duct -
# reads exactly like an opening. Neither the old rule nor the new one gets this right, and the old
# one is worse: taking the two largest planar faces makes that junction ring the outlet and seals
# BOTH real branches, where taking all of them at least leaves every real branch open.
#
# The function already accepts `opening_faces` to be told explicitly, and its own error message
# points there. What is missing is anyone passing it: the brief knows the user asked for one inlet
# and two outlets, and nothing compares that against what detection found. Until it does, an
# unblended junction yields one spurious port and no layer notices.
def test_a_flat_wall_face_is_still_mistaken_for_a_port(tmp_path):
    step = tmp_path / "y_unblended.step"

    import gmsh
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("y_raw")
        occ = gmsh.model.occ
        run = occ.addCylinder(0, 0, 0, 250, 0, 0, 30)
        th = math.radians(30)
        a = occ.addCylinder(250, 0, 0, 200 * math.cos(th), 200 * math.sin(th), 0, 20)
        b = occ.addCylinder(250, 0, 0, 200 * math.cos(th), -200 * math.sin(th), 0, 20)
        occ.fuse([(3, run)], [(3, a), (3, b)])          # no blend: the end-cap ring survives
        occ.synchronize()
        gmsh.write(str(step))
    finally:
        gmsh.finalize()

    out = tmp_path / "stls"
    out.mkdir()
    t = tessellate_internal(str(step), out, prepared=_prepared())

    # four "ports" for a three-port part: the extra one is the flat ring at the junction
    assert len(t["openings"]) == 4, t["openings"]
    areas = sorted(v["area"] * 1e6 for v in t["openings"].values())
    # two real branches, one real feed, and one impostor that is none of those sizes
    assert areas[0] == pytest.approx(math.pi * 20 ** 2, rel=0.02)
    assert areas[1] == pytest.approx(math.pi * 20 ** 2, rel=0.02)
    assert areas[3] == pytest.approx(math.pi * 30 ** 2, rel=0.02)
    impostor = areas[2]
    assert not any(impostor == pytest.approx(a, rel=0.02)
                   for a in (math.pi * 20 ** 2, math.pi * 30 ** 2)), impostor
