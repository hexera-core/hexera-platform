# Responsibility: Pin the classifier's requirements-near-miss branch: quality passed, a stated
# requirement measured short - the builder must be handed the gate's own diagnostic verbatim
# (never an empty or stale reviewer summary) and retry, not rebuild.
from __future__ import annotations

import pytest

from meshpipeline.pipeline.classifier import node_classifier

CAVEAT = [{"direction": "downstream", "requested": 8.0, "measured": 6.67,
           "units": "reference_lengths", "ruler_m": 0.06, "ruler_source": "user_stated"}]

DIAG = ("[DOMAIN_EXTENT_MISMATCH] downstream: requested 8L, mesh has 6.67L (tolerance 15%, "
        "measured in units of the approved reference length 0.06 m).")


@pytest.mark.asyncio
async def test_a_nearmiss_hands_the_builder_the_gates_diagnostic_verbatim():
    out = await node_classifier({
        "job_id": "j", "engine": "snappy", "retry_count": 1,
        "executor_success": True,
        "requirement_caveats": CAVEAT,
        "executor_output": DIAG,
        # stale reviewer facts from an earlier attempt must NOT leak into the summary
        "reviewer_feedback": "stale: layers too thin",
        "reviewer_axis_findings": [],
    })
    r = out["classifier_result"]
    assert r["error_source"] == "requirements_near_miss"
    assert r["failed_gate"] == "domain_extent"
    assert r["summary"] == DIAG
    assert "stale" not in r["summary"]
    assert out["builder_mode"] == "retry"


@pytest.mark.asyncio
async def test_a_clean_reviewer_fail_still_takes_the_reviewer_branch():
    out = await node_classifier({
        "job_id": "j", "engine": "snappy", "retry_count": 1,
        "executor_success": True,
        "requirement_caveats": [],
        "reviewer_feedback": "layer coverage 45.8% on the suction side",
        "reviewer_axis_findings": [],
    })
    assert out["classifier_result"]["error_source"] == "reviewer_fail"
    assert "45.8%" in out["classifier_result"]["summary"]
