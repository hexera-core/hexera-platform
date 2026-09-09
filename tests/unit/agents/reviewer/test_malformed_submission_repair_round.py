# Responsibility: Verify a malformed findings payload earns a repair round, and only repeating it stalls.
# Boundaries: the loop policy's progress accounting alone; the parser and eligibility are covered elsewhere.
# The live failure (bend_elbow_012_fluid, job 2d04696d): two inspect_region calls on a mesh with no
# declared regions, then a submission with evidence_ids missing on ONE axis - the third round in a
# row without progress, so the stall detector ended a review that held a valid mesh, with the
# round budget nine-tenths unused and the model never allowed to fix a one-field slip.
from __future__ import annotations

import asyncio
import types

from meshpipeline.agents.loop.accounting import ToolInvocation
from meshpipeline.agents.reviewer.loop_policy import ReviewLoopPolicy
from meshpipeline.contracts.agent_loop import LoopLimits, LoopTally
from meshpipeline.contracts.evidence_ledger import EvidenceLedger

AXES = ("surface_capture", "wake_resolution")


def _plan():
    return types.SimpleNamespace(
        axes=tuple(types.SimpleNamespace(name=n, owner="engine:snappy", requires=())
                   for n in AXES),
        engine="snappy", purpose="external_cfd",
        required_gate_keys=frozenset(), required_metric_keys=frozenset(),
        required_render_artifacts=frozenset(), required_targets=())


def _policy():
    return ReviewLoopPolicy(plan=_plan(), ledger=EvidenceLedger(), runtime=None,
                            limits_=LoopLimits(max_rounds=30, no_progress_threshold=3))


def _submit(policy, args, round_index=1):
    inv = ToolInvocation(round_index=round_index, call_index=1, tool="submit_findings",
                         raw_arguments="{}", parsed=args)
    return asyncio.run(policy.execute(inv))


def _missing_evidence_ids():
    # exactly the live payload shape: one axis complete, one without evidence_ids
    return {"axis_findings": [
        {"axis_key": "surface_capture", "finding": "captured", "evidence_ids": ["v-001"],
         "passed": True},
        {"axis_key": "wake_resolution", "finding": "resolved", "passed": True}],
        "rebuild_required": False, "reasoning": "r"}


def test_a_corrected_malformed_payload_is_the_rounds_progress():
    p = _policy()
    out = _submit(p, _missing_evidence_ids())
    assert out.accepted is False and "evidence_ids" in out.content
    assert p.submissions == 0 and p.malformed_submissions == 1
    seen = p.observe(LoopTally(rounds=1))
    assert seen.made_progress is True, "the correction is the round's progress: a repair round follows"
    assert seen.signature.startswith("malformed-corrected:")


def test_the_same_malformed_payload_again_is_a_stall():
    p = _policy()
    _submit(p, _missing_evidence_ids(), round_index=1)
    p.observe(LoopTally(rounds=1))
    _submit(p, _missing_evidence_ids(), round_index=2)
    seen = p.observe(LoopTally(rounds=2))
    assert seen.made_progress is False, "repeating the uncorrected payload is not progress"
    assert p.malformed_submissions == 2


def test_a_differently_malformed_payload_is_corrected_again():
    p = _policy()
    _submit(p, _missing_evidence_ids(), round_index=1)
    p.observe(LoopTally(rounds=1))
    other = {"axis_findings": [{"axis_key": "surface_capture", "finding": "x",
                                "evidence_ids": ["v-001"]}],          # passed missing now
             "rebuild_required": False, "reasoning": "r"}
    _submit(p, other, round_index=2)
    assert p.observe(LoopTally(rounds=2)).made_progress is True


def test_an_unparseable_payload_gets_the_same_repair_round():
    p = _policy()
    inv = ToolInvocation(round_index=1, call_index=1, tool="submit_findings",
                         raw_arguments="{not json", parsed=None)
    out = asyncio.run(p.execute(inv))
    assert out.accepted is False
    assert p.observe(LoopTally(rounds=1)).made_progress is True
    out = asyncio.run(p.execute(ToolInvocation(round_index=2, call_index=1,
                                               tool="submit_findings",
                                               raw_arguments="{still not", parsed=None)))
    assert out.accepted is False
    assert p.observe(LoopTally(rounds=2)).made_progress is False


def test_a_malformed_payload_is_still_not_a_submission_and_derives_no_verdict():
    p = _policy()
    _submit(p, _missing_evidence_ids())
    assert p.submissions == 0 and p.accepted is None and p.rejections == 0
