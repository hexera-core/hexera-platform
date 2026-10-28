# Responsibility: Verify the measured-quality line is built from the executor's own record (real figures from an FEA regression run's quality.json and a snappy layer replay's checkMesh), says what the numbers say, and passes the leak guard.
from __future__ import annotations

from meshpipeline.contracts import narration as N

# from a real FEA regression run's quality.json (oxlid_lab), the keys the line reads
FEA_CLEAN = {"cells": 4197, "min_sicn": 0.4572, "fea_inverted": 0, "elements_under_floor": 0,
             "fea_rules_short": 0, "fea": {"through_wall_min": 6.04}}
# from a real snappy layer replay's checkMesh (layercov_lab), parsed as foam_exec parses it
CFD_STEEP = {"cells": 2679296, "max_non_ortho": 67.9123, "max_skewness": 19.0431}


def test_a_clean_fea_mesh_reads_as_good_with_its_numbers():
    assert N.quality_line(FEA_CLEAN) == \
        "Quality looks good: 4,197 elements, worst element 0.46 (1 is perfect), every wall at least 6 elements thick."


def test_an_fea_mesh_with_notes_says_so_and_never_good():
    line = N.quality_line({**FEA_CLEAN, "min_sicn": 0.000601, "elements_under_floor": 1})
    assert line.startswith("The mesh is valid; some figures are past a limit")
    assert "worst element 0.0006" in line
    thin = N.quality_line({**FEA_CLEAN, "fea": {"through_wall_min": 1.4}})
    assert "the thinnest wall 1.4 elements thick" in thin and not thin.startswith("Quality looks good")


def test_inverted_elements_are_said_first():
    assert N.quality_line({**FEA_CLEAN, "fea_inverted": 3}) == \
        "3 elements are inside out, so no solver can use this mesh yet."


def test_a_cfd_mesh_with_steep_cells_says_the_solver_needs_care():
    line = N.quality_line(CFD_STEEP)
    assert line == ("The mesh is built, with a few steep cells the solver needs care with: 2.7M cells, "
                    "worst non-orthogonality 68°, worst skewness 19.")
    good = N.quality_line({"cells": 2081224, "max_non_ortho": 44.5, "max_skewness": 1.9})
    assert good == "Quality looks good: 2.1M cells, worst non-orthogonality 44°, worst skewness 1.9."


def test_nothing_measured_says_nothing():
    assert N.quality_line(None) is None
    assert N.quality_line({}) is None
    assert N.quality_line({"cells": 10}) is None


def test_every_quality_line_passes_the_guard():
    for q in (FEA_CLEAN, CFD_STEEP, {**FEA_CLEAN, "elements_under_floor": 2}, {**FEA_CLEAN, "fea_inverted": 1}):
        line = N.quality_line(q)
        assert line and not N.leaks(line), line
