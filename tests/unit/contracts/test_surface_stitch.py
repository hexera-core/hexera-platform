# Responsibility: Verify the word table that reads a stitch tolerance and a hole size from the user's notes (SURF-7,
# SURF-9, contracts/surface_stitch): the forms it reads, the units, "do not stitch", the latest word standing, and
# the words it must not read (a thickness, an element size, a hole that is not to be filled).
# Boundaries: pure text; no geometry.
from __future__ import annotations

import pytest

from meshpipeline.contracts import surface_stitch as ss


@pytest.mark.parametrize(("said", "mm"), [
    ("stitch gaps up to 0.5 mm", 0.5),
    ("Stitch gaps up to 0.5mm please", 0.5),
    ("close all gaps under 0.3 mm", 0.3),
    ("sew the surfaces within 0.2", 0.2),
    ("merge edges < 0.1 mm", 0.1),
    ("stitch up to 0.25 mm", 0.25),
    ("stitch tolerance 0.5 mm", 0.5),
    ("sewing tolerance = 0.1", 0.1),
    ("geometry clean-up tolerance: 0.2 mm", 0.2),
    ("gap tol 0.05", 0.05),
    ("tolerance for stitching 0.4 mm", 0.4),
    ("gaps up to 0.5 mm can be closed", 0.5),
    ("heal gaps up to 20 thou", 0.508),
    ("stitch gaps up to 0,5 mm", 0.5),
    ("stitch gaps up to 0.02 in", 0.508),
    ("1.2 mm thick, stitch gaps up to 0.5 mm, 4 mm elements", 0.5),
])
def test_a_stitch_tolerance_is_read_in_the_forms_people_write(said, mm):
    got = ss.read_text(said)
    assert got.mm == pytest.approx(mm) and not got.off and got.quote


@pytest.mark.parametrize("said", [
    "1.2 mm thick", "thickness 0.8 mm", "element size 4 mm", "about 25,000 elements", "close to 5 mm elements",
    "tolerance 0.1 mm on the hole positions", "the gap between the plates is 2 mm", "", "steel bracket, static load",
    "stitch gaps up to 500 mm",                               # no believable tolerance
])
def test_words_that_are_no_stitch_tolerance_are_not_read_as_one(said):
    got = ss.read_text(said)
    assert got.mm is None and not got.off


@pytest.mark.parametrize("said", [
    "do not stitch", "don't stitch the surfaces", "no stitching", "without sewing", "leave the gaps open",
    "do not close gaps", "keep gaps as they are", "no geometry cleanup",
])
def test_do_not_stitch_is_read(said):
    got = ss.read_text(said)
    assert got.off and got.mm is None and got.quote


@pytest.mark.parametrize(("said", "mm"), [
    ("ignore holes under 5 mm", 5.0),
    ("fill all holes smaller than 3mm", 3.0),
    ("remove small holes with diameter < 2 mm", 2.0),
    ("holes under 4 mm can be ignored", 4.0),
    ("holes smaller than 6 mm in diameter should be filled", 6.0),
    ("suppress holes below 0.25 in", 6.35),
    ("defeature pin holes up to 1.5 mm", 1.5),
])
def test_a_hole_size_is_read(said, mm):
    got = ss.read_text(said)
    assert got.holes_mm == pytest.approx(mm) and got.holes_quote and got.mm is None


@pytest.mark.parametrize("said", [
    "keep all holes", "the holes are 5 mm", "4 holes of 6 mm", "refine around holes under load", "ignore the 5 mm fillets",
    "mesh holes with 12 elements",
])
def test_a_hole_that_is_not_to_be_filled_is_not_read(said):
    assert ss.read_text(said).holes_mm is None


def test_a_hole_size_is_never_taken_for_the_stitch_tolerance():
    got = ss.read_text("close holes under 5 mm and stitch gaps up to 0.3 mm")
    assert got.holes_mm == 5.0 and got.mm == 0.3
    got = ss.read_text("close holes under 5 mm")
    assert got.holes_mm == 5.0 and got.mm is None


def test_the_latest_word_stands_for_each_on_its_own():
    got = ss.read(["stitch gaps up to 0.5 mm, ignore holes under 5 mm", "2 mm thick", "stitch gaps up to 0.2 mm"])
    assert got.mm == 0.2 and got.holes_mm == 5.0 and got.given
    got = ss.read(["stitch gaps up to 0.5 mm", "do not stitch"])
    assert got.off and got.mm is None
    got = ss.read(["do not stitch", "ok, stitch gaps up to 0.1 mm"])
    assert got.mm == 0.1 and not got.off
    assert not ss.read(["1 mm thick", ""]).given
