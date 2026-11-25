# The reader was stricter about a material than about a part.
#
# MEASURED 2026-11-25 on the real chip.ecxml: <vendorKModel>table7</vendorKModel> inserted beside
# <orthotropic> in ONE of its 33 materials refused the whole upload - HTTP 422, "materials/
# AID_UF.1/thermalConductivity: it must hold exactly one of isotropic, orthotropic,
# linearTemperatureDependant, nonLinearTemperatureDependant" - and the message named the material
# but not the element to remove. The IDENTICAL element inside a <solid3dBlock> is counted by
# `_extensions` and tolerated, so a tool that annotates its conductivity models could not upload at
# all while the same annotation on its geometry was fine.
#
# The module's own comment says tags are read loosely on purpose, because vendors write what they
# like where the schema allows xs:any. These tests hold the two sides of that: a vendor child
# beside ONE kind is counted and read, and a file with no kind at all - or two - is still refused,
# now naming what it found.
import pytest

from meshpipeline.cad.ingest.ecxml import EcxmlError, read_ecxml

_BLOCK = """                <solid3dBlock>
                    <name>B1</name>
                    <active>true</active>
                    <location>
                        <x>0</x><y>0</y><z>0</z>
                    </location>
                    <size>
                        <x>0.01</x><y>0.01</y><z>0.01</z>
                    </size>
                    <material>copper</material>
                    <powerDissipation>1</powerDissipation>
                </solid3dBlock>
"""


def _file(conductivity, *, block=_BLOCK):
    return ('<?xml version="1.0" encoding="utf-8"?>\n<neutralXML>\n'
            "    <name>one block</name>\n"
            "    <producer>Icepak</producer>\n"
            "    <materials>\n        <material>\n"
            "            <name>copper</name>\n"
            "            <density>8933</density>\n"
            "            <specific_heat>385</specific_heat>\n"
            "            <surfaceEmissivity>0</surfaceEmissivity>\n"
            "            <thermalConductivity>\n" + conductivity +
            "            </thermalConductivity>\n"
            "        </material>\n    </materials>\n"
            "    <geometry>\n        <assembly>\n            <name>A</name>\n"
            "            <active>true</active>\n            <geometry>\n" + block +
            "            </geometry>\n        </assembly>\n    </geometry>\n"
            "</neutralXML>\n")


_ISO = ("                <isotropic>\n"
        "                    <conductivity>387.6</conductivity>\n"
        "                </isotropic>\n")
_ORTHO = ("                <orthotropic>\n"
          "                    <x>74</x><y>74</y><z>5</z>\n"
          "                </orthotropic>\n")
_VENDOR = "                <vendorKModel>table7</vendorKModel>\n"


def _written(tmp_path, text, name="m.ecxml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_a_vendor_annotation_beside_the_model_is_read_not_refused(tmp_path):
    model = read_ecxml(_written(tmp_path, _file(_ORTHO + _VENDOR)))
    assert model.materials["copper"].conductivity["kind"] == "orthotropic"
    assert model.materials["copper"].conductivity["W_mK"] == [74.0, 74.0, 5.0]


def test_the_annotation_is_counted_exactly_as_one_on_a_part_would_be(tmp_path):
    model = read_ecxml(_written(tmp_path, _file(_ISO + _VENDOR)))
    assert model.ignored_elements.get("thermalConductivity/vendorKModel") == 1, (
        model.ignored_elements)


def test_the_annotation_may_come_first(tmp_path):
    # Document order is tolerated everywhere else in this reader; it has to be here too.
    model = read_ecxml(_written(tmp_path, _file(_VENDOR + _ISO)))
    assert model.materials["copper"].conductivity["kind"] == "isotropic"


def test_two_conductivity_models_are_still_refused_and_both_are_named(tmp_path):
    with pytest.raises(EcxmlError) as got:
        read_ecxml(_written(tmp_path, _file(_ISO + _ORTHO)))
    msg = str(got.value)
    assert "2 of them" in msg, msg
    assert "<isotropic>" in msg and "<orthotropic>" in msg, msg


def test_a_material_with_no_model_at_all_is_refused_and_told_what_it_holds(tmp_path):
    with pytest.raises(EcxmlError) as got:
        read_ecxml(_written(tmp_path, _file(_VENDOR)))
    msg = str(got.value)
    assert "<vendorKModel>" in msg, msg
    assert "none of those" in msg, msg
    assert "materials/copper/thermalConductivity" in msg, msg


def test_an_empty_conductivity_says_it_is_empty(tmp_path):
    with pytest.raises(EcxmlError) as got:
        read_ecxml(_written(tmp_path, _file("")))
    assert "it is empty" in str(got.value), str(got.value)


def test_the_missing_name_message_states_the_line_once(tmp_path):
    nameless = _BLOCK.replace("                    <name>B1</name>\n", "")
    with pytest.raises(EcxmlError) as got:
        read_ecxml(_written(tmp_path, _file(_ISO, block=nameless)))
    msg = str(got.value)
    assert msg.count("on line") == 1, msg
    assert "has no <name>" in msg, msg
    assert msg.startswith("A/<solid3dBlock>"), msg
