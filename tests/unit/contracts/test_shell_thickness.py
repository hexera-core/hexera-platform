# Responsibility: Verify SURF-4's word table (contracts/shell_thickness): the ways a user writes a wall thickness are
# read into millimetres; look-alikes (a temperature, a time, an element size, a layer or coating thickness, a bare
# gauge number, a range) are not; the latest word stands; other thicknesses named are handed back, never applied.
# Boundaries: pure text; no geometry.
from __future__ import annotations

import pytest

from meshpipeline.contracts import shell_thickness as st


@pytest.mark.parametrize(("text", "mm"), [
    ("1.2 mm thick", 1.2), ("mesh it as a shell, 1.2mm-thick steel", 1.2), ("t = 0.8", 0.8), ("t=0.8mm", 0.8),
    ("gauge 2 mm", 2.0), ("thickness 1.5", 1.5), ("thickness of 0.9 mm", 0.9), ("Wall thickness: 2,5 mm", 2.5),
    ("the sheet is 0.8 mm thick, DC04", 0.8), ("2 mm gauge sheet", 2.0), ("1.5mm wall", 1.5), ("thk 3", 3.0),
    ("0.05 in thick", 1.27), ("thickness = 1 cm", 10.0), ("20 thou thick", 0.508), ("thickness is about 1.2 mm", 1.2),
    ("plate thickness 6mm", 6.0), ("3 mm plate", 3.0), ("shell thickness 0.7 mm", 0.7), ("2mm thk", 2.0),
    ("THICKNESS 0.75 MM", 0.75), ("0.9 thick", 0.9),
])
def test_a_thickness_in_the_users_words_is_read_in_millimetres(text, mm):
    got = st.read_text(text)
    assert got.given and got.mm == pytest.approx(mm)
    assert got.quote and got.quote.lower() in " ".join(text.split()).lower()


@pytest.mark.parametrize("text", [
    "T = 300 K", "t = 0.8 s", "T=20", "first layer thickness 0.01 mm", "element size 3 mm", "use 5 mm plate elements",
    "mesh size 2 mm, quads", "coating thickness 0.02 mm", "part = 3", "about 4000 elements", "", "FEA, static",
    "paint thickness 80 um", "thickness 12 layers",
])
def test_what_only_looks_like_a_thickness_is_not_read(text):
    assert not st.read_text(text).given


def test_a_bare_gauge_number_and_a_range_are_handed_back_unread():
    assert st.read_text("16 gauge steel").unread == "a gauge number (16 gauge)"
    assert st.read_text("gauge 16").unread == "a gauge number (gauge 16)"
    got = st.read_text("1.5-2 mm thick")
    assert not got.given and got.unread.startswith("a range")
    # a thickness far past a metre is not believed
    assert not st.read_text("thickness 5000").given


def test_the_latest_word_stands_and_other_thicknesses_are_listed_not_applied():
    assert st.read(["shell please, 1 mm thick", "actually thickness 1.2 mm", "and use quads"]).mm == 1.2
    got = st.read_text("outer panel 0.8 mm thick, reinforcement t = 1.5")
    assert got.mm == 0.8 and got.others_mm == (1.5,)
    # the same value said twice is one thickness
    assert st.read_text("1 mm thick, thickness 1.0 mm").others_mm == ()
    assert st.read(["", None]).mm is None


def test_a_runs_words_are_the_summary_then_the_notes():
    state = {"request_txt": "FEA of a bracket, thickness 2 mm",
             "user_notes": [{"id": "n1", "text": "the sheet is 1.2 mm thick"}, {"id": "n2", "text": "quads please"}]}
    assert st.texts_of(state) == ["FEA of a bracket, thickness 2 mm", "the sheet is 1.2 mm thick", "quads please"]
    assert st.read(st.texts_of(state)).mm == 1.2
    assert st.texts_of(None) == []
    assert st.words(1.2) == "1.2 mm" and st.words(0.05) == "0.05 mm" and st.words(12) == "12 mm"
