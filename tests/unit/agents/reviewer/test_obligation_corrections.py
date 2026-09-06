# Responsibility: Pin the two message contracts that broke on the thin-plate review (job
# b555d446): an uninspected required region must be demanded as an IMPERATIVE naming the tool
# to run, and the loop's correction message must demand evidence - never claim "not more
# evidence" - while target obligations remain unmet.
from __future__ import annotations

from meshpipeline.agents.reviewer.loop_policy import AxisDeficits


class TestObligationAwareCorrections:
    def _body(self, deficits):
        # exercise the real correction renderer without standing up a full session
        from meshpipeline.agents.reviewer.loop_policy import ReviewLoopPolicy
        policy = ReviewLoopPolicy.__new__(ReviewLoopPolicy)
        return policy._correction_body(deficits, unchanged=False)

    def test_unmet_obligations_demand_evidence_not_corrected_findings(self):
        deficits = AxisDeficits(
            unmet_obligations=(
                "required region 'slice_x_centre' has not been inspected in this session. "
                "It IS available - inspect it now",))
        body = self._body(deficits)
        assert "slice_x_centre" in body
        assert "REQUIRED" in body
        # the exact sentence that deadlocked the thin-plate review must never accompany an
        # outstanding evidence obligation
        assert "not more evidence" not in body

    def test_axis_only_deficits_keep_the_corrected_finding_message(self):
        deficits = AxisDeficits(duplicate=("surface_capture",))
        body = self._body(deficits)
        assert "not more evidence" in body

    def test_obligations_count_toward_the_progress_signature(self):
        # gathering the missing inspection must register as progress, or the stall detector
        # kills a review that is actively correcting itself
        before = AxisDeficits(unmet_obligations=("required region 'slice_x_centre'...",))
        after = AxisDeficits()
        assert before.signature() != after.signature()
        assert not before.clean and after.clean


class TestTheDeficitReadsAsAnOrder:
    def test_uninspected_target_message_is_imperative_and_names_the_tool(self):
        from meshpipeline.agents.reviewer.eligibility import _check_obligation
        from meshpipeline.contracts.evidence_ledger import EvidenceLedger, TargetKind

        class Obligation:
            kind = TargetKind.REGION
            exact_ids = ("slice_x_centre",)
            min_count = 1
            provenance = "authored inspection regions"

        reasons = _check_obligation(Obligation(), EvidenceLedger())
        assert len(reasons) == 1
        msg = reasons[0]
        # the old phrasing ("has no usable inspection") was read by a reviewer as "this region
        # is not available" - the reworded message must state availability and the action
        assert "has not been inspected" in msg
        assert "IS available" in msg
        assert "slice tool" in msg
        assert "cite" in msg
        assert "has no usable inspection" not in msg
