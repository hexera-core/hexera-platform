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
        "Quality looks good: 4,197 elements, worst element 0.457 (1 is perfect), every wall at least 6 elements thick."


def test_an_fea_mesh_with_notes_says_so_and_never_good():
    line = N.quality_line({**FEA_CLEAN, "min_sicn": 0.000601, "elements_under_floor": 1})
    assert line.startswith("The mesh is valid; some figures are past a limit")
    assert "worst element 0.000601" in line
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


def test_the_worst_element_is_the_scaled_jacobian_the_result_screen_shows():
    # dev-areen 2026-10-30 (job 94b8c76d): the live line said 0.33 (the SICN) while the result
    # screen and the Mesh report said worst scaled Jacobian 0.198
    q = {"cells": 409108, "min_sicn": 0.3301, "fea_min_scaled_jacobian": 0.198, "fea_inverted": 0,
         "elements_under_floor": 0, "fea_rules_short": 0, "fea": {"through_wall_min": 2.0}}
    assert N.quality_line(q) == \
        "Quality looks good: 409k elements, worst element 0.198 (1 is perfect), every wall at least 2 elements thick."


def test_the_live_line_reads_the_verdicts_limits_not_its_own():
    # demo dry run 2026-11-02 (C5 knuckle): the line said "Quality looks good ... worst element 0.00286"
    # while the verdict listed that very figure under its 0.01 limit; the floor and the rules were clean
    knuckle = {"cells": 1017710, "min_sicn": 0.12, "fea_min_scaled_jacobian": 0.002861, "fea_inverted": 0,
               "elements_under_floor": 0, "fea_rules_short": 0, "fea": {"through_wall_min": 2.03}}
    said = N.quality_line(knuckle, past_limits=["Worst scaled Jacobian (1 is a perfect element)"])
    assert said.startswith("The mesh is valid; some figures are past a limit") and "0.00286" in said
    assert N.quality_line(knuckle, past_limits=[]).startswith("Quality looks good")


def test_the_executor_hands_the_line_the_rows_the_verdict_misses():
    from meshpipeline.pipeline.executor import _fea_past_limits

    q = {"cells": 1017710, "element_order": 2, "min_sicn": 0.12, "fea_inverted": 0, "elements_under_floor": 0,
         "fea_rules_short": 0, "fea_min_scaled_jacobian": 0.002861, "fea_max_jacobian_ratio": 3.0,
         "fea_max_aspect_ratio": 153.6, "fea_min_dihedral_deg": 1.628, "fea_max_dihedral_deg": 176.8,
         "fea": {"plan": "fea_plan.json", "through_wall_min": 2.03}}
    past = _fea_past_limits(q)
    assert past is not None and len(past) == 4
    assert any("scaled Jacobian" in p for p in past) and any("aspect" in p for p in past)
    clean = {**q, "fea_min_scaled_jacobian": 0.2, "fea_max_aspect_ratio": 9.0, "fea_min_dihedral_deg": 12.0,
             "fea_max_dihedral_deg": 150.0}
    assert _fea_past_limits(clean) == []
    assert N.quality_line(clean, past_limits=_fea_past_limits(clean)).startswith("Quality looks good")
    # a CFD record is not an FEA plan's: the line keeps its own reading
    assert _fea_past_limits({"cells": 10, "max_non_ortho": 40.0}) is None
