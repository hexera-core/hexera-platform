# "the schema requires it greater than 0" read as pedantry on four kinds where it is physics.
#
# Four object kinds carry a <plane> and are NOT in the 2D set: axial3dFan, printedCircuitBoard,
# solidCylinder and twoResistorModel. A flat one of those is refused with the generic size
# message, which says the rule and not the reason - and a thermal tool that exported a flat
# 2-resistor package meant something by it.
#
# MEASURED 2026-11-25: accepting them instead is NOT an option. With ZERO_SIZE_ALLOWED widened at
# runtime, a flat printedCircuitBoard reaches `_occ_box` and raises Standard_DomainError out of
# OCC, because the fused build makes these with `_box_maker` and a box of no thickness is not a
# solid. A flat grille is fine for the opposite reason: it goes to `_face_object` and never
# becomes a solid. So the refusal is right and only its wording was wrong.
import pytest

from meshpipeline.cad.ingest import ecxml as EX
from meshpipeline.cad.ingest.ecxml import EcxmlError, read_ecxml

EXTRA = {
    "printedCircuitBoard": "<plane>+xy</plane>",
    "twoResistorModel": "<plane>+xy</plane><thetaJC>1</thetaJC><thetaJB>2</thetaJB>",
    "solidCylinder": "<plane>+xy</plane>",
    "axial3dFan": "<plane>+xy</plane>",
}


def _file(kind, extra="", z="0"):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<neutralXML>\n'
            "  <name>t</name>\n  <producer>Icepak</producer>\n"
            "  <materials><material><name>copper</name><density>1</density>"
            "<specific_heat>1</specific_heat><surfaceEmissivity>0</surfaceEmissivity>"
            "<thermalConductivity><isotropic><conductivity>1</conductivity></isotropic>"
            "</thermalConductivity></material></materials>\n"
            "  <geometry><assembly><name>A</name><active>true</active><geometry>\n"
            f"    <{kind}>\n      <name>X1</name>\n      <active>true</active>\n"
            "      <location><x>0</x><y>0</y><z>0</z></location>\n"
            f"      <size><x>0.01</x><y>0.01</y><z>{z}</z></size>\n"
            "      <material>copper</material>\n      <powerDissipation>1</powerDissipation>\n"
            f"      {extra}\n    </{kind}>\n"
            "  </geometry></assembly></geometry>\n</neutralXML>\n")


def _refusal(tmp_path, kind, z="0"):
    p = tmp_path / f"{kind}.ecxml"
    p.write_text(_file(kind, EXTRA.get(kind, ""), z), encoding="utf-8")
    with pytest.raises(EcxmlError) as got:
        read_ecxml(p)
    return str(got.value)


def test_the_four_kinds_that_need_a_thickness_are_exactly_the_plane_carriers_outside_the_2d_set():
    # If this drifts, a kind is being refused with no reason or given one it does not need.
    plane = {k for k, fields in EX.OBJECT_FIELDS.items() if "plane" in fields}
    assert set(EX._NEEDS_THICKNESS) == plane - EX.TWO_D, sorted(plane - EX.TWO_D)


def test_the_zero_size_rule_itself_is_untouched():
    # The fix is wording. Widening this set crashes OCC, which is why it must not move.
    assert EX.ZERO_SIZE_ALLOWED == EX.TWO_D


@pytest.mark.parametrize("kind, says", [
    ("printedCircuitBoard", "through-plane conductivity"),
    ("twoResistorModel", "thetaJC leaves through its case face"),
    ("solidCylinder", "the cylinder's axis"),
    ("axial3dFan", "a depth the air passes through"),
])
def test_a_flat_one_is_told_what_the_number_is_for(tmp_path, kind, says):
    said = _refusal(tmp_path, kind)
    assert says in said, said


def test_the_original_sentence_is_still_the_start_of_it(tmp_path):
    # Other tests assert on substrings of the generic message; it stays, and the reason follows.
    said = _refusal(tmp_path, "printedCircuitBoard")
    assert said.startswith("A/X1/size: <z> on line"), said
    assert "the schema requires it greater than 0" in said, said


def test_a_kind_with_no_reason_declared_is_refused_exactly_as_before(tmp_path):
    said = _refusal(tmp_path, "solid3dBlock")
    assert said.endswith("the schema requires it greater than 0"), said


def test_a_flat_2d_object_is_still_perfectly_fine(tmp_path):
    p = tmp_path / "flat.ecxml"
    p.write_text(_file("solid2dBlock", "<plane>+xy</plane>"), encoding="utf-8")
    model = read_ecxml(p)
    # the assembly is an object too, so count the block itself
    flat = [o for o in model.objects if o.kind == "solid2dBlock"]
    assert len(flat) == 1 and flat[0].size[2] == 0.0, model.objects


def test_one_with_a_real_thickness_is_read_without_a_word(tmp_path):
    p = tmp_path / "thick.ecxml"
    p.write_text(_file("printedCircuitBoard", EXTRA["printedCircuitBoard"], z="0.0016"),
                 encoding="utf-8")
    model = read_ecxml(p)
    board = [o for o in model.objects if o.kind == "printedCircuitBoard"]
    assert len(board) == 1 and board[0].size[2] == 0.0016, model.objects
    assert model.notes == []
