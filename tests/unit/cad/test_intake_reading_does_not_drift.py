"""What the intake reads off a shape, pinned, so semiconductor work cannot quietly move it.

The measuring step, the unit reading and the word reader are SHARED by every upload, and the
semiconductor work changed all three. Nothing held them to their previous answers, and the cost of
that showed up immediately: a perforated-face change meant to find a cold plate's channels turned
a car with a grille and a rear vent from "solid body in external flow" into "pipe wall, internal
flow" at confidence 0.9. The lab's own car cases could not have caught it - every road-vehicle row
but one is an STL and the perforated reading is B-rep only - and neither could anything in CI,
because no vehicle geometry is committed anywhere in the repository.

So this pins the CATEGORICAL reading - what the part is, which way the fluid goes, how many
openings - for the committed fixtures and for four shapes built here that cover the cases the
thresholds turn on. Areas and centroids are deliberately NOT pinned: they are measurements, they
move with the tessellator, and pinning them would make this file noise.

MEASURED before and after the semiconductor changes (commits 3c54df91..ce50ffd1), field for field
over all eight shapes: every reading below is unchanged. The only field that moved at all was
wants_fluid_volume, on the four single solids, which is the new gate and is the point of it. The
word reader moved on exactly two of ten phrases, both intended: "mesh this cold plate" and "150 W
processor with a fan" went from asking the domain question to routing to electronics cooling.

WHAT THE NEW SPLIT COSTS, measured the same way, against a 120 s budget:

    transition_rect_to_round    scout 0.17s -> 0.24s   plus 0.79s of split
    flange_bored_8bolts         scout 0.14s -> 0.04s   plus 0.70s
    pipe_tee_single_solid       scout 0.04s -> 0.04s   plus 0.63s
    plain_bluff_box             scout 0.01s -> 0.01s   plus 0.60s
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "geometry"

#: name -> (body_kind, input_kind, flow, openings). None means "the scout declines to read it",
#: which is a reading too.
COMMITTED = {
    "cht_enclosing_2region.step": ("hollow_wall", "body-surface", "internal", 6),
    "plate_with_hole_2d.step": ("surface", "body-surface", "internal", 1),
    "transition_rect_to_round.step": ("pipe_wall", "body-surface", "internal", 2),
}


def _scouted(path: Path):
    from meshpipeline.cad.scout import scout_cad
    from meshpipeline.cad.unit_evidence import parser_applied_unit
    from meshpipeline.contracts.coordinate_state import from_occ_transfer
    from meshpipeline.contracts.geometry_units import (
        GeometryInterpretation,
        LengthUnit,
        ResolutionBasis,
        scale_to_metres,
    )
    interp = GeometryInterpretation(
        interpretation_id="t", owner_id="t", geometry_source_id="t",
        unit=LengthUnit.millimetre, scale_to_metres=scale_to_metres(LengthUnit.millimetre),
        basis=ResolutionBasis.file_declared, evidence="t")
    return scout_cad(path, prepared=from_occ_transfer(interp, parser_applied_unit(path)))


def _reading(r) -> tuple:
    return (r.body_kind, r.input_kind, r.flow, len(r.openings))


# #
# the files that ship with the repository
# #
@pytest.mark.parametrize(("name", "want"), sorted(COMMITTED.items()))
def test_a_committed_fixture_reads_the_same_as_it_always_did(name, want):
    path = FIXTURES / name
    if not path.is_file():
        pytest.skip(f"{name} is not committed")
    assert _reading(_scouted(path)) == want


# #
# and four shapes that cover what the thresholds turn on
# #
@pytest.fixture(scope="module")
def built(tmp_path_factory) -> dict:
    """A bored flange, the same disc with a bolt circle and no bore, a single-solid pipe tee, and
    a plain bluff box. The first two are the pair the perforated-face rule has to separate; the
    tee is the shape that caught an over-eager override; the box is the control."""
    pytest.importorskip("OCP", reason="OpenCASCADE is not available")
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    def z_cyl(x, y, z, r, h):
        return BRepPrimAPI_MakeCylinder(
            gp_Ax2(gp_Pnt(x, y, z), gp_Dir(0.0, 0.0, 1.0)), r, h).Shape()

    def bolt_holes(solid):
        for k in range(8):
            a = 2.0 * math.pi * k / 8.0
            solid = BRepAlgoAPI_Cut(
                solid, z_cyl(75.0 * math.cos(a), 75.0 * math.sin(a), -1.0, 5.0, 22.0)).Shape()
        return solid

    # a 200 mm disc, 20 thick: a 100 mm bore takes 93% of the hole area, so the bore stays the
    # opening on its own however many bolt holes sit beside it
    flange = bolt_holes(BRepAlgoAPI_Cut(z_cyl(0, 0, 0, 100.0, 20.0),
                                        z_cyl(0, 0, -1.0, 50.0, 22.0)).Shape())
    # ...and with no bore at all, eight equal holes and nothing dominant: perforated, but with no
    # second face carrying the same count, so not a passage
    circle = bolt_holes(z_cyl(0, 0, 0, 100.0, 20.0))

    tee = BRepAlgoAPI_Cut(z_cyl(0, 0, 0, 40.0, 300.0), z_cyl(0, 0, -1.0, 30.0, 302.0)).Shape()
    tee = BRepAlgoAPI_Fuse(tee, BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0.0, 0.0, 150.0), gp_Dir(1.0, 0.0, 0.0)), 40.0, 200.0).Shape()).Shape()
    tee = BRepAlgoAPI_Cut(tee, BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0.0, 0.0, 150.0), gp_Dir(1.0, 0.0, 0.0)), 30.0, 201.0).Shape()).Shape()

    box = BRepPrimAPI_MakeBox(gp_Pnt(0.0, 0.0, 0.0), 400.0, 160.0, 120.0).Shape()

    out = tmp_path_factory.mktemp("shapes")
    paths = {}
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    for name, shape in (("flange_bored_8bolts", flange), ("bolt_circle_no_bore", circle),
                        ("pipe_tee_single_solid", tee), ("plain_bluff_box", box)):
        dest = out / f"{name}.step"
        w = STEPControl_Writer()
        w.Transfer(shape, STEPControl_AsIs)
        assert w.Write(str(dest)) == IFSelect_ReturnStatus.IFSelect_RetDone
        paths[name] = dest
    return paths


BUILT = {
    "flange_bored_8bolts": ("pipe_wall", "body-surface", "internal", 2),
    "bolt_circle_no_bore": ("single_solid", "solid-body", "external", 0),
    "pipe_tee_single_solid": ("pipe_wall", "body-surface", "internal", 3),
    "plain_bluff_box": ("single_solid", "solid-body", "external", 2),
}


@pytest.mark.parametrize("name", sorted(BUILT))
def test_a_built_shape_reads_as_it_did(name, built):
    assert _reading(_scouted(built[name])) == BUILT[name]


def test_a_bolt_circle_is_not_a_passage(built):
    """The case the perforated rule has to get right in BOTH directions. Eight equal holes on one
    face are perforated by area - no single hole dominates - but nothing on the disc carries the
    same count, so they are a bolt circle rather than the end of eight channels."""
    r = _scouted(built["bolt_circle_no_bore"])
    assert r.flow == "external"
    assert r.openings == [] or all(not o.perforated for o in r.openings)


def test_a_bore_is_still_the_opening_beside_its_bolt_holes(built):
    r = _scouted(built["flange_bored_8bolts"])
    assert r.flow == "internal"
    assert len(r.openings) == 2
    assert all(not o.perforated for o in r.openings), "a bore is not a perforated face"


# #
# the words, pinned the same way
# #
#: what the user typed -> (route, ask). Two of these moved with the semiconductor work, on
#: purpose; the rest are the control.
SAID = {
    "mesh this cold plate": ("electronics", ""),
    "cool this chip": ("electronics", ""),
    "150 W processor with a fan": ("electronics", ""),
    "drag on this car body": ("flow", ""),
    "external aero on a car at 30 m/s": ("flow", ""),
    "pressure drop through this manifold": ("flow", ""),
    "mesh the water inside this pipe": ("flow", ""),
    "500 W pump, what is the flow rate": ("flow", ""),
    "stress in this bracket": ("fea", ""),
    "thermal stress in this disc": ("fea", ""),
}


@pytest.mark.parametrize(("said", "want"), sorted(SAID.items()))
def test_the_word_reader_sends_it_where_it_always_did(said, want):
    from meshpipeline.application import purpose_map as pm
    r = pm.read_purpose(said)
    assert (r.route, r.ask) == want, r.why
