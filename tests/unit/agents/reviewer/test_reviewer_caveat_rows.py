# Responsibility: Verify the reviewer brief states every adjudicated caveat in words and never crashes on a missing figure.
from __future__ import annotations

from meshpipeline.agents.reviewer.visual import _with_adjudicated_deviations


def test_a_note_gate_caveat_without_direction_figures_does_not_crash():
    """lp_base, dev-areen 25 Oct: a resolution_policy caveat (no requested/measured) reached the
    reviewer and f"{None:g}" failed the whole job after a valid 3-hour mesh."""
    caveat = {"kind": "resolution_short", "gate": "narrow_gap_resolution", "engine": "gmsh",
              "cells_across": 2.0, "floor": 3.0, "cells": 1_200_000, "cell_limit": 8_000_000}
    out = _with_adjudicated_deviations("BRIEF", [caveat])
    assert "narrow_gap_resolution: 2 cells across where 3 were asked" in out
    assert "1.2e+06 cells against a limit of 8e+06" in out


def test_a_per_direction_caveat_keeps_its_old_words():
    out = _with_adjudicated_deviations("BRIEF", [{"direction": "x", "requested": 0.5, "measured": 0.45}])
    assert "x: requested 0.5, measured 0.45 reference-lengths" in out


def test_missing_figures_are_said_as_missing_not_formatted():
    out = _with_adjudicated_deviations("BRIEF", [{"direction": "y", "requested": None, "measured": 0.2},
                                                  {"gate": "cell_limit"}, "not a dict"])
    assert "y: requested n/a, measured 0.2" in out and "cell_limit" in out


def test_a_cfd_note_gate_caveat_and_a_domain_extent_caveat_together():
    """The reviewer node is shared: a CFD run (snappy) reaches it with the same resolution_policy caveats
    beside the domain-extent ones (direction / requested / measured), and must state both."""
    from meshpipeline.engines.resolution_policy import caveat

    note = caveat("narrow_gap_resolution", {"cells_across": 4.0, "needed": 12.0, "cells": 3_000_000},
                  {"cell_count": 3_000_000}, engine="snappyHexMesh")
    extent = {"direction": "+x", "requested": 10.0, "measured": 8.5, "units": "reference_lengths"}
    out = _with_adjudicated_deviations("BRIEF", [note, extent])
    assert "+x: requested 10, measured 8.5 reference-lengths" in out
    assert "4 cells across where 12 were asked" in out
