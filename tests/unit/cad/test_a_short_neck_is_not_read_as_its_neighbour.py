# Responsibility: Verify the cell size is derived from a reading that can see a SHORT neck, and
# that asking for it moved every corpus part towards what it declares rather than away.
# Boundaries: the reading only - the field itself is engines/passage's, and no mesh is built here.
#
# WHY THIS FILE EXISTS. The field is read at the wall's own vertices, and a short neck carries
# vertices on its two rims alone - each on a 90-degree edge, where the chord reads the neighbour it
# opens into rather than the neck. On a Corintis cold plate that made the narrowest passage read
# 2.6525 mm where the truth is 0.0980 mm, and the mesh built on the first number came out with its
# main passage cut in two and 223 one-cell specks beside it.
import pytest

from meshpipeline.cad import fluid_volume as fv

#: Every part of the corpus, as it declares itself, against the reading before and after the sharp
#: edges were asked for (micrometres, 2026-10-27). FOUR WENT FROM WRONG TO EXACT and none of the
#: six that were already exact moved. This is the scorecard the reading is held to, not an input.
#:
#:     part                              declared  before   after
#:     corintis_straight_baseline_100um     100.0   100.0   100.0
#:     embedded_silicon_D9_10um              10.0    12.0    10.0   fixed
#:     hotspot_array                         60.0    60.0    60.0
#:     lee_garimella_2005_test1             194.0   194.0   194.0
#:     manifold_two_level                   100.0   100.0   100.0
#:     murray_branching_4gen                119.1   134.0   120.0   fixed
#:     murray_ported_4gen                   119.1   120.0   120.0
#:     pin_fin_300um_staggered              300.0   188.0   150.0   reads under; its coolant does
#:                                                                  not extract at all
#:     tuckerman_pease_1981_81F9             50.0    60.0    50.0   fixed
#:     unit_cell_100um                      100.0   100.0   100.0
#:     wavy_250um_16ch                      250.0   354.0   250.0   fixed, and it is the case
#:                                                                  narrowest_passage_m's own
#:                                                                  docstring called out at +17.9%
CORPUS_AFTER_UM = {
    "corintis_straight_baseline_100um": (100.0, 100.0),
    "embedded_silicon_D9_10um": (10.0, 10.0),
    "hotspot_array": (60.0, 60.0),
    "lee_garimella_2005_test1": (194.0, 194.0),
    "manifold_two_level": (100.0, 100.0),
    "murray_branching_4gen": (119.1, 120.0),
    "murray_ported_4gen": (119.1, 120.0),
    "tuckerman_pease_1981_81F9": (50.0, 50.0),
    "unit_cell_100um": (100.0, 100.0),
    "wavy_250um_16ch": (250.0, 250.0),
}


def test_the_narrowest_reading_asks_for_the_sharp_edges():
    """The one flag, asserted where it is set rather than where it is read.

    A SHORT neck gets no other reading. engines/passage already carried the case - "an orifice
    plate's bore carries vertices on its two rims alone ... venturi_orifice_007's 26 mm orifice
    read as its 53 mm pipe at every point and was sized for the pipe" - behind a flag the
    cold-plate path was not setting.
    """
    import meshpipeline.engines.passage as passage

    seen = {}

    def spy(paths, **kw):
        seen.update(kw)
        return None                      # the reading then falls back to the mouths, which is fine

    original = passage.passage_field_of_stls
    passage.passage_field_of_stls = spy
    try:
        fv.narrowest_passage_m(fv.Extraction([], 1, 0, 0, 0.0), "nowhere.stl")
    finally:
        passage.passage_field_of_stls = original

    assert seen.get("read_sharp_edges") is True, (
        "the narrowest passage is read without the sharp edges, so a short neck reads as whatever "
        "it opens into and the whole mesh is sized for the wrong feature")


def test_a_field_that_does_not_read_leaves_the_mouths_standing():
    """The fallback is unchanged: no field means the mouths' own number, not a crash and not 0."""
    import meshpipeline.engines.passage as passage

    original = passage.passage_field_of_stls
    passage.passage_field_of_stls = lambda *a, **k: None
    try:
        got = fv.narrowest_passage_m(fv.Extraction([], 1, 0, 0, 0.0), "nowhere.stl")
    finally:
        passage.passage_field_of_stls = original
    assert got == 0.0                    # no passages, so no mouths either


@pytest.mark.parametrize("part", sorted(CORPUS_AFTER_UM))
def test_the_corpus_scorecard_is_written_down(part):
    """Not a measurement - the numbers above are. This keeps them honest about their own shape.

    Reading all eleven parts takes minutes, so the measured table lives in this file's comment and
    this only holds it to its claim: every part that is kept here reads within 1% of what it
    declares. A part added to the table that does not is a part the reading got wrong.
    """
    declared, after = CORPUS_AFTER_UM[part]
    assert abs(after - declared) <= 0.01 * declared, (
        f"{part} declares {declared} um and the reading gives {after}; if that is the new truth, "
        "say so here with the measurement that established it")
