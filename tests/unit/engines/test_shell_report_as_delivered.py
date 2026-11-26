# Responsibility: Hold SHMESH-85: the report's asked-against-delivered counts a figure AT a line (within the digits the
# deck carries) as inside it, so the build's report and a reader of the delivered deck agree.
# Boundaries: numpy on a made-up mesh; no gmsh.
from __future__ import annotations

import numpy as np
from tests.unit.engines.test_shell_requirements import _mesh, criteria

from meshpipeline.engines import shell_requirements as SR


def test_an_edge_made_exactly_at_the_shortest_edge_is_inside_it():
    # squares of 10 mm, and quads 5 mm wide whose 5 mm edges come out 4.9999999999 here (and 5.0000000001 in a deck)
    narrow = [[(0, 0, 0), (10, 0, 0), (10, 5 - 1e-10, 0), (0, 5 - 1e-10, 0)]]
    square = [[(0, 0, 0), (10, 0, 0), (10, 10, 0), (0, 10, 0)]]
    rep = SR.report(_mesh(square * 8 + narrow * 2), criteria())
    shortest = next(r for r in rep["rows"] if r["field"] == "min_edge")
    assert shortest["outside"] == 0 and shortest["met"] is True
    # a real shortfall still counts
    short = [[(0, 0, 0), (10, 0, 0), (10, 4.99, 0), (0, 4.99, 0)]]
    rep = SR.report(_mesh(square * 8 + short * 2), criteria())
    assert next(r for r in rep["rows"] if r["field"] == "min_edge")["outside"] == 2


def test_a_figure_at_a_quality_line_is_inside_it():
    v = SR._at(np.array([135.0 - 1e-9, 135.0 + 1e-9, 136.0, 30.0 + 1e-8]), 135.0, 150.0, 30.0)
    assert v.tolist() == [135.0, 135.0, 136.0, 30.0]
