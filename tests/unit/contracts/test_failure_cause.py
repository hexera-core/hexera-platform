# Responsibility: Verify each failure cause is told in its own plain sentence, and which causes a retry can change.
from __future__ import annotations

import pytest

from meshpipeline.contracts.failure_cause import (
    RETRY_SKIPPED_NOTE,
    SEAM_CAUSES,
    FailureCause,
    describe,
    retry_can_help,
)

#: The words the old one-size headline used. No cause may fall back on them: a naming mismatch
#: was reported as "did not meet the required quality checks" (job ac1daa3e).
_OLD_HEADLINE = "required quality checks"


def _say(cause, **facts) -> tuple[str, str]:
    what, nxt = describe(cause, facts, engine="snappyHexMesh")
    assert what, f"{cause} must say what failed"
    # a refused geometry's one sentence is errors.py's, and it carries its own next step
    assert nxt or cause == FailureCause.GEOMETRY_REJECTED, f"{cause} must say what to do next"
    assert _OLD_HEADLINE not in what + nxt
    return what, nxt


class TestEachCauseHasItsOwnSentence:
    def test_the_car_wall_case_names_both_spellings_and_the_way_round_it(self):
        what, nxt = _say(FailureCause.CONTRACT_MISMATCH, missing=["car wall"],
                         renamed={"car wall": "car_wall"},
                         present=["car_wall", "farfield", "ground"], before_meshing=True)
        assert "'car wall'" in what and "'car_wall'" in what
        assert "our mistake, not your geometry's" in what
        assert "before meshing" in what and "no time was spent" in what
        assert "quality" not in what
        assert "rename 'car wall' to 'car_wall'" in nxt and "We will fix this" in nxt

    def test_a_missing_boundary_lists_what_the_mesh_has_instead(self):
        what, nxt = _say(FailureCause.CONTRACT_MISMATCH, missing=["inlet", "outlet"],
                         present=["car_wall", "farfield"])
        assert "no boundary called 'inlet' or 'outlet'" in what
        assert "'car_wall' and 'farfield'" in what
        assert "different boundary setup" in nxt

    def test_a_wrong_type_is_named(self):
        what, _ = _say(FailureCause.CONTRACT_MISMATCH,
                       mistyped=[{"name": "sym", "declared": "symmetry", "got": "patch"}])
        assert "'sym'" in what and "'patch'" in what and "'symmetry'" in what

    def test_a_lost_boundary_is_a_meshing_problem_not_ours(self):
        what, nxt = _say(FailureCause.PATCH_NOT_CAPTURED, patches=["inlet_2"])
        assert "lost the boundary 'inlet_2'" in what and "no faces" in what
        assert "our mistake" not in what
        assert "finer cells" in nxt

    def test_a_boundary_of_the_wrong_type(self):
        what, _ = _say(FailureCause.BOUNDARY_TYPE, mistyped=[
            {"name": "frontAndBack", "declared": "empty", "want": "empty", "got": "patch"}])
        assert "'frontAndBack' came out as type 'patch'" in what and "'empty'" in what

    def test_poor_quality_states_the_number_and_the_limit(self):
        what, nxt = _say(FailureCause.MESH_QUALITY, checks=[{
            "key": "skew_fraction", "label": "Skewed faces are localized",
            "measured": 0.0021, "op": "<=", "threshold": 5e-4}])
        assert "0.21% of the faces are badly skewed" in what and "at most 0.05%" in what
        assert "run it again" in nxt

    def test_non_orthogonality_is_said_in_degrees(self):
        what, _ = _say(FailureCause.MESH_QUALITY, checks=[{
            "key": "max_non_ortho", "label": "Max face non-orthogonality",
            "measured": 78.4, "op": "<=", "threshold": 65.0}])
        assert "78 degrees" in what and "limit is 65 degrees" in what

    def test_element_quality_names_its_floor(self):
        what, _ = _say(FailureCause.MESH_QUALITY, checks=[{
            "key": "min_sicn", "label": "element quality (SICN)", "measured": 0.004,
            "op": ">=", "threshold": 0.01}])
        assert "0.004" in what and "at least 0.01" in what

    def test_broken_cells_are_named(self):
        what, _ = _say(FailureCause.MESH_QUALITY, fatal=["negative-volume cells"])
        assert "broken cells (negative-volume cells)" in what

    def test_an_unmeasured_bar_is_ours_not_the_geometry(self):
        what, _ = _say(FailureCause.MESH_QUALITY, unmeasured=["tetrahedron quality"])
        assert "could not measure 'tetrahedron quality'" in what and "on our side" in what

    def test_under_resolution_gives_the_count(self):
        what, _ = _say(FailureCause.UNDER_RESOLVED, cells_across=7.2, needed=12)
        assert "about 7.2 cells across" in what and "at least 12" in what

    def test_over_budget_gives_both_counts(self):
        what, nxt = _say(FailureCause.CELL_BUDGET, cells=14_200_000, limit=12_000_000)
        assert "14,200,000 cells" in what and "12,000,000-cell limit" in what
        assert "less detail" in nxt

    def test_a_short_domain_gives_the_numbers(self):
        what, _ = _say(FailureCause.DOMAIN_EXTENT, before_meshing=True, misses=[
            {"direction": "downstream", "requested": 10.0, "measured": 4.0},
            {"direction": "vertical", "requested": 3.0, "measured": 0.0}])
        assert "downstream is 4 reference lengths where you asked for 10" in what
        assert "touches the body on the vertical side" in what
        assert "before meshing" in what

    def test_an_engine_that_stopped_is_named(self):
        what, _ = _say(FailureCause.ENGINE_CRASHED)
        assert "snappyHexMesh" in what and "on our side" in what

    def test_a_refused_geometry_is_worded_once_by_the_domain_class(self):
        # one vocabulary: the cause says what errors.DOMAIN_REJECTED says, nothing of its own
        from meshpipeline.errors import FailureClass, user_message_for
        what, _nxt = _say(FailureCause.GEOMETRY_REJECTED,
                          reason="[GEOMETRY_UNSUITABLE] the input surface self-intersects.")
        assert what == user_message_for(FailureClass.DOMAIN_REJECTED,
                                        reason="the input surface self-intersects")
        assert "[" not in what

    @pytest.mark.parametrize("cause", list(FailureCause))
    def test_every_cause_has_words_even_with_no_facts(self, cause):
        _say(cause)

    def test_an_unknown_cause_says_nothing_rather_than_guess(self):
        assert describe("not-a-cause", {}) == ("", "")


class TestWhatARetryCanChange:
    @pytest.mark.parametrize("cause", [FailureCause.CONTRACT_MISMATCH,
                                       FailureCause.GEOMETRY_REJECTED])
    def test_a_deterministic_cause_is_not_retried(self, cause):
        assert retry_can_help(cause) is False

    @pytest.mark.parametrize("cause", [c for c in FailureCause
                                       if c not in (FailureCause.CONTRACT_MISMATCH,
                                                    FailureCause.GEOMETRY_REJECTED)])
    def test_a_meshing_cause_keeps_its_retries(self, cause):
        assert retry_can_help(cause) is True

    def test_no_cause_keeps_the_old_ladder(self):
        assert retry_can_help("") is True and retry_can_help(None) is True
        assert retry_can_help("something new") is True

    def test_names_the_builder_chose_can_be_fixed_by_the_builder(self):
        # gmsh's groups are written by the model - a mismatch there is not deterministic
        assert retry_can_help(FailureCause.CONTRACT_MISMATCH, {"retry_may_fix": True}) is True

    def test_the_skip_is_said_in_plain_words(self):
        assert "did not try again" in RETRY_SKIPPED_NOTE

    def test_every_executor_seam_has_a_cause(self):
        assert set(SEAM_CAUSES) == {"finalize", "domain_extent", "solvability", "geometry"}
