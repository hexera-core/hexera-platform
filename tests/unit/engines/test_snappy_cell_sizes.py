# Responsibility: Verify the snappy plan's base and finest wall cell are read from the authored case's own numbers (the background box over its divisions, halved once per surface level) and never guessed.
from __future__ import annotations

from meshpipeline.engines.snappy.drivers import _cell_sizes


def test_the_base_cell_is_the_box_over_its_divisions_and_the_wall_cell_halves_per_level():
    base, wall = _cell_sizes({"domain_min": [-8, -4, 0], "domain_max": [16, 4, 4],
                              "divisions": [150, 50, 25], "surface_level": [3, 5]})
    assert round(base, 6) == 0.16 and round(wall, 6) == 0.005


def test_a_case_without_its_numbers_gives_no_sizes():
    assert _cell_sizes({}) == (None, None)
    assert _cell_sizes({"domain_min": [0, 0, 0], "domain_max": [1, 1, 1], "divisions": [0, 0, 0],
                        "surface_level": 2}) == (None, None)
    assert _cell_sizes(None) == (None, None)


def test_the_run_note_says_the_sizes_in_millimetres_never_a_refinement_level():
    from meshpipeline.contracts.narration import leaks
    from meshpipeline.engines.snappy.drivers import _sizes_words

    words = _sizes_words({"domain_min": [-8, -4, 0], "domain_max": [16, 4, 4],
                          "divisions": [150, 50, 25], "surface_level": [3, 5]})
    assert words == "base cells about 160 mm, down to about 5 mm at the walls, "
    assert "level" not in words and not leaks(words)
    assert _sizes_words({}) == ""
