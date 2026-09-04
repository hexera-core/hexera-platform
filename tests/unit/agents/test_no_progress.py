# Responsibility: Pin the retry ladder's no-progress stop. The combining-wye burned four
# attempts on a byte-identical rejection no re-plan could fix; the ladder must halt the moment
# the same gate rejects for the same reason twice, and must NEVER halt productive iteration
# (different failures, or reviewer-driven refinement, which carries no gate signature).
from __future__ import annotations

from meshpipeline.agents.builder.no_progress import (
    failure_signature,
    record_failure,
    repeats_previous,
)


def ws(tmp_path, n: int):
    d = tmp_path / f"attempt_{n}"
    d.mkdir(parents=True, exist_ok=True)
    return d


# "failed_gate" is the key the classifier ACTUALLY writes (pipeline/classifier.py). The
# earlier fixtures used "gate" - the same wrong key the reader used - so the test agreed
# with the bug and passed while the stop never fired in production. These fixtures use the
# real key now, so the test exercises the true classifier->no_progress contract.
WYE_FAILURE = {"classifier_result": {
    "failed_gate": "manifest_valid", "section": "MANIFEST",
    "summary": "these patches have zero faces: ['inlet_1', 'inlet_2', 'outlet']"}}


class TestTheLadderHaltsOnIdenticalFailures:
    def test_the_wye_trajectory_stops_at_the_second_identical_rejection(self, tmp_path):
        # attempt 2 was caused by failure F; attempt 3 is being prepared for the same F
        sig2 = failure_signature(WYE_FAILURE)
        record_failure(ws(tmp_path, 2), sig2)
        w3 = ws(tmp_path, 3)
        sig3 = failure_signature(WYE_FAILURE)
        record_failure(w3, sig3)
        assert repeats_previous(w3, sig3)

    def test_a_different_failure_is_progress(self, tmp_path):
        record_failure(ws(tmp_path, 2), failure_signature(WYE_FAILURE))
        w3 = ws(tmp_path, 3)
        other = failure_signature({"classifier_result": {
            "failed_gate": "domain_extent", "section": "DOMAIN",
            "summary": "downstream 6.67L vs 8L"}})
        record_failure(w3, other)
        assert not repeats_previous(w3, other)

    def test_reviewer_driven_retries_carry_no_signature_and_never_stop(self, tmp_path):
        # a reviewer FAIL retries via reviewer_feedback with no gate - that iteration is
        # productive (the manifold improved layers 45.8% -> 68.8% and DELIVERED on attempt 3)
        state = {"classifier_result": {}, "reviewer_feedback": "layers too thin on the wall"}
        assert failure_signature(state) is None
        w3 = ws(tmp_path, 3)
        record_failure(w3, None)
        assert not repeats_previous(w3, None)

    def test_signature_reads_the_exact_key_the_classifier_writes(self):
        # REGRESSION: the reader looked up "gate" while the classifier writes "failed_gate",
        # so this returned None on every executor failure and the stop never fired. Build the
        # classifier_result the way pipeline/classifier.py actually does and require a
        # signature. Pinning the real key stops the reader/writer contract from drifting again.
        from meshpipeline.pipeline import classifier as C
        import inspect
        src = inspect.getsource(C)
        assert '"failed_gate"' in src and '"gate":' not in src, (
            "the classifier's gate key changed - update no_progress.failure_signature to match")
        cr = {"section": "MANIFEST", "summary": "zero faces", "failed_gate": "manifest_valid"}
        sig = failure_signature({"classifier_result": cr})
        assert sig is not None and sig["gate"] == "manifest_valid"

    def test_the_first_retry_never_stops(self, tmp_path):
        w2 = ws(tmp_path, 2)
        sig = failure_signature(WYE_FAILURE)
        record_failure(w2, sig)
        assert not repeats_previous(w2, sig)  # attempt_1 recorded nothing

    def test_missing_or_corrupt_sibling_record_never_stops(self, tmp_path):
        ws(tmp_path, 2)  # exists but has no failure record
        w3 = ws(tmp_path, 3)
        sig = failure_signature(WYE_FAILURE)
        record_failure(w3, sig)
        assert not repeats_previous(w3, sig)
        (tmp_path / "attempt_2" / ".last_failure.json").write_text("{broken")
        assert not repeats_previous(w3, sig)

    def test_a_non_attempt_workspace_never_stops(self, tmp_path):
        d = tmp_path / "workspace"
        d.mkdir()
        sig = failure_signature(WYE_FAILURE)
        record_failure(d, sig)
        assert not repeats_previous(d, sig)
