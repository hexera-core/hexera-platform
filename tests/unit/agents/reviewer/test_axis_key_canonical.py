# Responsibility: Verify an axis key filed under its category is read as the axis, and a key that
# names no axis is called out by name in the rejection.
# volute_scroll_005 (job 9188dc4b): the reviewer filed every axis as 'conformance/group_completeness'
# and so on; the keys matched nothing, the deficit check ignored them, and six identical rejections
# named the same seven axes as missing without once saying the keys were wrong. A valid 662,004-cell
# mesh ended with no verdict.
from __future__ import annotations

import asyncio
import types

from meshpipeline.agents.loop.accounting import ToolInvocation
from meshpipeline.agents.reviewer.eligibility import AxisFinding
from meshpipeline.agents.reviewer.loop_policy import (
    ReviewLoopPolicy,
    canonical_axis_key,
    compute_deficits,
)
from meshpipeline.contracts.agent_loop import LoopLimits
from meshpipeline.contracts.evidence_ledger import EvidenceLedger

AXES = ("surface_capture", "wake_resolution")


def _plan(*names):
    return types.SimpleNamespace(
        axes=tuple(types.SimpleNamespace(name=n, owner="engine:snappy", requires=())
                   for n in (names or AXES)),
        engine="snappy", purpose="external_cfd",
        required_gate_keys=frozenset(), required_metric_keys=frozenset(),
        required_render_artifacts=frozenset(), required_targets=())


def _policy(*axes):
    return ReviewLoopPolicy(plan=_plan(*axes), ledger=EvidenceLedger(), runtime=None,
                            limits_=LoopLimits(max_rounds=30, no_progress_threshold=3))


def _submit(policy, findings):
    args = {"axis_findings": [{"axis_key": k, "finding": t, "evidence_ids": list(e), "passed": True}
                              for k, t, e in findings],
            "rebuild_required": False, "reasoning": "r"}
    inv = ToolInvocation(round_index=1, call_index=1, tool="submit_findings",
                         raw_arguments="{}", parsed=args)
    return asyncio.run(policy.execute(inv))


def test_a_category_prefixed_key_names_its_axis():
    assert canonical_axis_key("conformance/group_completeness", ("group_completeness",)) == "group_completeness"
    assert canonical_axis_key("quality:element_quality_distribution",
                              ("element_quality_distribution",)) == "element_quality_distribution"
    assert canonical_axis_key("Surface-Capture", AXES) == "surface_capture"
    assert canonical_axis_key("surface_capture", AXES) == "surface_capture"


def test_a_key_that_names_no_axis_stays_as_filed_and_is_reported():
    assert canonical_axis_key("conformance/nonsense", AXES) == "conformance/nonsense"
    L = EvidenceLedger()
    e = L.add_render_view("iso", "k", "open", image_ok=True)
    d = compute_deficits(AXES, (AxisFinding("conformance/nonsense", "x", (e,)),), L)
    assert d.unknown == ("conformance/nonsense",)
    assert d.missing == AXES


def test_the_live_payload_is_accepted_once_the_keys_are_read_as_axes():
    # exactly what the reviewer filed on volute_scroll_005, against this rubric's two axes
    p = _policy()
    e = p.ledger.add_render_view("iso", "k", "open", image_ok=True)
    out = _submit(p, [("conformance/surface_capture", "captured", (e,)),
                      ("quality/wake_resolution", "resolved", (e,))])
    assert out.terminal is True and "accepted" in out.content.lower()
    assert p.accepted is not None
    assert {f.axis_key for f in p.accepted_findings} == set(AXES)


def test_the_rejection_names_the_wrong_key_and_the_exact_required_keys():
    p = _policy()
    e = p.ledger.add_render_view("iso", "k", "open", image_ok=True)
    out = _submit(p, [("conformance/nonsense", "x", (e,))])
    assert out.accepted is False
    assert "Unknown axis keys" in out.content and "conformance/nonsense" in out.content
    assert "surface_capture, wake_resolution" in out.content
