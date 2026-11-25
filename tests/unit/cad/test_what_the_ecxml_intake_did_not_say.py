# What the ECXML intake did not say.
#
# The product's own intake offers ECXML as the way to bring a whole electronics-cooling model in:
# "every part, its power and the fans read from the file". That is a promise about ANY such export,
# and 55 variants of the real customer file found the parser itself robust - every failure came out
# as a sentence naming the element and the line, never a traceback or a bare 500. What broke the
# promise was silence. Three measurements, 2026-11-25:
#
#   * <producer>6SigmaET</producer> put a note on the model AND on the placement, and the user saw
#     neither, because `thermal_facts` built its notes from the VIEW's warnings only. The same
#     silence hid "the heatsink lists no <parts>, so it has no blocks to build and is left out" -
#     a part REMOVED FROM THE MESH.
#   * every location and size multiplied by 1000, as a millimetre export would be, was read as a
#     70000 x 78615 x 3549 mm part with a warning list byte-identical to the correct file's. ECXML
#     has no way to declare a unit (attributes are discarded unread) and the check records
#     unit_assumed False, which switches off the question that catches a mis-scaled STEP.
#   * a second root <geometry> holding one 5 W copper block was read as 57 objects - unchanged -
#     with no note and no ignored element, and the user was told "The model's total power is 0 W:
#     nothing in it gives off heat."
import pytest

from meshpipeline.application.geometry_check import thermal_facts
from meshpipeline.application.thermal_view import thermal_view
from meshpipeline.cad.ingest.ecxml import EcxmlError, read_ecxml

_MATERIAL = """        <material>
            <name>copper</name>
            <density>8933</density>
            <specific_heat>385</specific_heat>
            <surfaceEmissivity>0</surfaceEmissivity>
            <thermalConductivity>
                <isotropic>
                    <conductivity>387.6</conductivity>
                </isotropic>
            </thermalConductivity>
        </material>
"""

_BLOCK = """                <solid3dBlock>
                    <name>{name}</name>
                    <active>true</active>
                    <location>
                        <x>0</x><y>0</y><z>0</z>
                    </location>
                    <size>
                        <x>{s}</x><y>{s}</y><z>{s}</z>
                    </size>
                    <material>copper</material>
                    <powerDissipation>{w}</powerDissipation>
                </solid3dBlock>
"""


def _file(*, producer="Icepak", size=0.01, watts=1, extra_geometry=False, repeat=""):
    """One block in one assembly, with whatever the test is about bolted on."""
    geometry = ("    <geometry>\n        <assembly>\n            <name>A</name>\n"
                "            <active>true</active>\n            <geometry>\n"
                + _BLOCK.format(name="B1", s=size, w=watts)
                + "            </geometry>\n        </assembly>\n    </geometry>\n")
    second = ("    <geometry>\n        <assembly>\n            <name>SECOND</name>\n"
              "            <active>true</active>\n            <geometry>\n"
              + _BLOCK.format(name="SECOND_BLOCK", s=size, w=5)
              + "            </geometry>\n        </assembly>\n    </geometry>\n")
    return ('<?xml version="1.0" encoding="utf-8"?>\n<neutralXML>\n'
            "    <name>one block</name>\n"
            f"    <producer>{producer}</producer>\n"
            + repeat
            + "    <materials>\n" + _MATERIAL + "    </materials>\n"
            + geometry + (second if extra_geometry else "")
            + "</neutralXML>\n")


def _written(tmp_path, text, name="m.ecxml"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _view(path):
    from meshpipeline.cad.ingest.ecxml_place import place, placement_sidecar

    placement = place(read_ecxml(path))
    return thermal_view(placement_sidecar(placement), placement), placement


class _Canonical:
    """What geometry_check hands thermal_facts: the reader's notes travel on it."""

    def __init__(self, notes):
        self.notes = list(notes)

    def facts(self):
        return {"geometry_kind": "thermal", "source_format": "ecxml"}


# ----------------------------------------------------------------- the reader's notes are shown
def test_a_note_the_reader_wrote_reaches_the_user(tmp_path):
    path = _written(tmp_path, _file(producer="6SigmaET"))
    model = read_ecxml(path)
    assert any("6SigmaET" in n for n in model.notes), model.notes
    view, _pl = _view(path)
    facts = thermal_facts(view, _Canonical(model.notes))
    assert any("6SigmaET" in n for n in facts["notes"]), facts["notes"]


def test_a_part_left_out_of_the_mesh_is_not_left_out_of_the_notes():
    # The worst of the notes: the reader DROPS a part and says so only here.
    gone = ("A/hs1: the heatsink lists no <parts>, so it has no blocks to build and is left out")
    facts = thermal_facts({"title": "t", "domain": {"size_mm": [10.0, 10.0, 10.0]},
                           "warnings": []}, _Canonical([gone]))
    assert gone in facts["notes"]


def test_the_views_own_warnings_still_travel_and_are_not_doubled():
    shared = "the same sentence"
    facts = thermal_facts({"title": "t", "domain": {"size_mm": [10.0, 10.0, 10.0]},
                           "warnings": [{"level": "warn", "text": shared},
                                        {"level": "warn", "text": "a view warning"},
                                        {"level": "info", "text": "not a note"}]},
                          _Canonical([shared, "a reader note"]))
    assert facts["notes"].count(shared) == 1, facts["notes"]
    assert "a view warning" in facts["notes"]
    assert "a reader note" in facts["notes"]
    assert "not a note" not in facts["notes"]


# ----------------------------------------------------------------- a size that cannot be right
def test_a_millimetre_export_read_as_metres_is_said_out_loud(tmp_path):
    # 0.07 becomes 70 in a file a tool wrote in millimetres, which is what the real customer
    # file did when every length was multiplied by 1000: a 70 m part.
    view, _pl = _view(_written(tmp_path, _file(size=70.0)))
    texts = [w["text"] for w in view["warnings"] if w["level"] == "warn"]
    assert any("not a size an electronics-cooling model has" in t for t in texts), texts
    assert any("thousand times too large" in t for t in texts), texts


def test_the_warning_reaches_the_form_the_user_reads(tmp_path):
    view, _pl = _view(_written(tmp_path, _file(size=70.0)))
    facts = thermal_facts(view, _Canonical([]))
    assert any("electronics-cooling model" in n for n in facts["notes"]), facts["notes"]


def test_a_model_a_thousand_times_too_small_is_said_too(tmp_path):
    view, _pl = _view(_written(tmp_path, _file(size=1e-06)))
    texts = [w["text"] for w in view["warnings"] if w["level"] == "warn"]
    assert any("thousand times too small" in t for t in texts), texts


def test_the_real_sizes_of_a_cold_plate_and_a_chip_package_say_nothing(tmp_path):
    # The band has to be wide enough to stay quiet on a real model: a 10 mm block, a 1 m rack,
    # and a 10 m hall, which is the edge of the band and must still be read as a model.
    for size in (0.01, 1.0, 10.0):
        view, _pl = _view(_written(tmp_path, _file(size=size), name=f"m{size}.ecxml"))
        texts = [w["text"] for w in view["warnings"]]
        assert not any("electronics-cooling model has" in t for t in texts), (size, texts)


def test_the_band_is_the_one_the_measurement_asked_for():
    # Imported here and not at the top: a missing constant must fail THIS test, not stop
    # the file being collected and take the other ten with it.
    from meshpipeline.application.thermal_view import PLAUSIBLE_MAX_MM, PLAUSIBLE_MIN_MM

    assert PLAUSIBLE_MAX_MM == 10_000.0      # 10 m: a rack and a cabinet are inside it
    assert PLAUSIBLE_MIN_MM == 0.01          # 10 um: under a single metallisation layer


# ----------------------------------------------------------------- a second copy is not dropped
def test_a_second_geometry_is_refused_rather_than_silently_dropped(tmp_path):
    path = _written(tmp_path, _file(extra_geometry=True))
    with pytest.raises(EcxmlError) as got:
        read_ecxml(path)
    msg = str(got.value)
    assert "2 <geometry>" in msg, msg
    assert "dropped" in msg, msg


def test_a_repeated_description_is_a_note_and_not_a_refusal(tmp_path):
    # Nothing is lost by reading the first <name>, so this must not refuse the file.
    path = _written(tmp_path, _file(repeat="    <name>a second title</name>\n"))
    model = read_ecxml(path)
    assert model.objects
    assert any("<name>" in n and "ignored" in n for n in model.notes), model.notes


def test_one_of_each_is_still_read_without_a_word(tmp_path):
    model = read_ecxml(_written(tmp_path, _file()))
    assert list(model.notes) == [], model.notes


def test_a_size_too_big_to_write_out_is_still_readable(tmp_path):
    # My own first cut of this warning printed a 1e300 coordinate as 300 grouped digits inside a
    # sentence telling the user their part was too big. Past a trillion the magnitude IS the
    # message. (This file is ACCEPTED - nothing refuses it - so the sentence is all the user gets.)
    view, _pl = _view(_written(tmp_path, _file(size=1e300), name="huge.ecxml"))
    said = " ".join(w["text"] for w in view["warnings"] if w["level"] == "warn")
    assert "electronics-cooling model" in said, said
    assert "e+30" in said, said
    assert "," not in said.split("comes out")[1].split("mm")[0], said


def test_a_real_size_still_reads_as_a_number_a_person_writes(tmp_path):
    from meshpipeline.application.thermal_view import _num

    assert _num(177.5) == "177.5"
    assert _num(70000.0) == "70000"
    assert _num(70000000000.0) == "70,000,000,000"
