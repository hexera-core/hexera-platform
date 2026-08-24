# Responsibility: Pin the caveated route: a quality-passing mesh with requirement caveats
# retries for FULL conformance while attempts remain (delivery-with-caveats is the ladder's
# floor, never its shortcut), proceeds to review when the ladder is spent, and a clean pass
# routes exactly as it always has.
from __future__ import annotations

from meshpipeline.pipeline.graph import route_after_executor


def st(**over):
    base = {"executor_success": True, "retry_count": 0, "solvability_failed": False,
            "requirement_caveats": [], "job_id": "j"}
    base.update(over)
    return base


CAVEAT = [{"direction": "downstream", "requested": 8.0, "measured": 6.67,
           "units": "reference_lengths", "ruler_m": 0.06, "ruler_source": "user_stated"}]


class TestCaveatedRouting:
    def test_a_clean_pass_goes_straight_to_review(self):
        assert route_after_executor(st()) == "node_reviewer"

    def test_a_caveated_pass_with_retries_remaining_retries_for_conformance(self):
        assert route_after_executor(st(requirement_caveats=CAVEAT)) == "node_classifier"

    def test_a_caveated_pass_with_the_ladder_spent_proceeds_to_review(self):
        import meshpipeline.agents.builder.settings as bcfg
        spent = bcfg.MAX_BUILDER_RETRIES + 1
        assert route_after_executor(
            st(requirement_caveats=CAVEAT, retry_count=spent)) == "node_reviewer"

    def test_a_failing_executor_still_routes_exactly_as_before(self):
        assert route_after_executor(st(executor_success=False)) == "node_classifier"
        import meshpipeline.agents.builder.settings as bcfg
        spent = bcfg.MAX_BUILDER_RETRIES + 1
        assert route_after_executor(
            st(executor_success=False, retry_count=spent)) == "__end__"
