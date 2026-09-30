# Responsibility: Prove a review is not ended by asking for a boundary the viewer cannot draw.
# Boundaries: the review loop, its policy and its words, driven by a scripted model and a viewer double.
# The live failure (Windsor body, job 53bbce4b, 2026-09-30): a 2.49M-cell snappyHexMesh mesh passed
# every gate and a trial solve. The reviewer looked at the body, zoomed, asked to show 'ground' (no
# review geometry), cut a slice, then asked to frame 'farfield' from the front, the rear and the
# side. Each farfield call was refused as "unknown patch" and counted as a round without progress,
# so the third ended the review at round 7 of 60 with no verdict, and the job failed as "something
# went wrong on our side". The recorded tool sequence is replayed here, in order.
from __future__ import annotations

import asyncio
import json

import pytest

from meshpipeline.agents.loop import diagnostics
from meshpipeline.agents.reviewer.loop_policy import REVIEWER_NO_PROGRESS_THRESHOLD
from meshpipeline.agents.reviewer.render_runtime import TypedToolResult
from meshpipeline.agents.reviewer.unified import run_unified_review
from meshpipeline.contracts.evidence_ledger import EvidenceLedger
from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
from meshpipeline.contracts.review_evidence import EvidenceItem

RENDERABLE = ("Windsor_body",)

# REAL, from the job's event stream: the reviewer's seven calls, with their arguments
RECORDED = [
    ("go_to_coordinates", {"x": -0.03952, "y": -0.002508, "z": 0.2375, "span": 1.054,
                           "preset": "iso", "patch_name": "Windsor_body"}),
    ("zoom_to_region", {"screen_x": 0.5, "screen_y": 0.5, "magnification": 8}),
    ("toggle_patch", {"patch_name": "ground", "visible": True}),
    ("inspect_region", {"region_name": "nearwall_z"}),
    ("go_to_coordinates", {"x": -5.86153, "y": 0.0, "z": 0.0, "span": 2.0, "preset": "front",
                           "patch_name": "farfield"}),
    ("go_to_coordinates", {"x": 11.0775, "y": 0.0, "z": 0.0, "span": 2.0, "preset": "rear",
                           "patch_name": "farfield"}),
    ("go_to_coordinates", {"x": 0.0, "y": -5.51162, "z": 0.0, "span": 2.0, "preset": "left",
                           "patch_name": "farfield"}),
]
AXES = ("surface_capture", "farfield_clearance", "domain_enclosure")


class _Ax:
    def __init__(self, name):
        self.name = name
        self.requires = ()


class _Plan:
    engine = "snappy"
    purpose = "external_cfd"
    required_gate_keys = frozenset()
    required_metric_keys = frozenset()
    required_render_artifacts = frozenset()
    required_targets = ()

    def __init__(self):
        self.axes = tuple(_Ax(a) for a in AXES)


class _Opening:
    has_geometry = True
    initial_screenshot_b64 = ""
    inspection_targets = ()
    nav_context = ""
    patch_colour_legend = ()
    patch_views = ()
    patch_names = list(RENDERABLE)


class _Viewer:
    """The reviewer runtime as the Windsor job saw it: only the body carries review geometry. A
    patch without it is refused before the renderer, exactly as render_runtime now screens it."""

    def __init__(self):
        self.n = 0
        self.rendered: list[str] = []

    async def execute_typed(self, fn, args):
        patch = args.get("patch_name")
        if patch and patch not in RENDERABLE:
            return TypedToolResult(
                content=(f"Patch '{patch}' carries no review geometry and cannot be shown. "
                         f"Renderable patches: {', '.join(RENDERABLE)}."),
                evidence=None, is_viewer_tool=True)
        self.n += 1
        self.rendered.append(fn)
        return TypedToolResult(
            content=[{"type": "text", "text": f"{fn} rendered."}],
            evidence=EvidenceItem(evidence_id="", seq=self.n, label=fn, purpose="inspect",
                                  image_ref=f"/tmp/windsor-{self.n}.png", view_id=f"v{self.n}",
                                  artifact_key="mesh_paths.surface"),
            is_viewer_tool=True)


def _call(i, name, args):
    return ToolCallRequest(id=f"c-{i}", name=name, arguments=json.dumps(args))


def _submission(evidence_ids):
    return {"axis_findings": [
        {"axis_key": a, "finding": "judged from the measured numbers and the views",
         "evidence_ids": list(evidence_ids), "passed": True} for a in AXES],
        "rebuild_required": False, "reasoning": "every axis grounded"}


@pytest.fixture
def records(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(diagnostics, "emit",
                        lambda rec: out.append(diagnostics.sanitized(rec)) or out[-1])
    return out


def _review(script, *, max_rounds=60):
    seen_messages: list[list] = []

    async def provider(**kw):
        seen_messages.append(list(kw.get("messages") or []))
        i = len(seen_messages) - 1
        name, args = script[i] if i < len(script) else script[-1]
        return ModelRoundResult(tool_calls=(_call(i, name, args),), finish_reason="tool_calls")

    viewer = _Viewer()
    out = asyncio.run(run_unified_review(
        plan=_Plan(), ledger=EvidenceLedger(), runtime=viewer, opening=_Opening(),
        system_prompt="s", opening_context_text="c", provider_call=provider,
        max_rounds=max_rounds, total_timeout_s=1800.0, job_id="job-windsor-replay", attempt=1))
    return out, seen_messages, viewer


def test_the_recorded_windsor_review_is_not_ended_by_the_boundaries_it_cannot_see(records):
    # the seven recorded calls, then the submission the model can make from what it has
    script = [*RECORDED, ("submit_findings", _submission(["v-001", "v-002", "v-003"]))]
    out, _msgs, viewer = _review(script)
    assert out.api_failure == "", f"the review ended without a verdict ({out.api_failure})"
    assert out.verdict == "PASS"
    assert out.llm_rounds == len(RECORDED) + 1
    assert viewer.rendered == ["go_to_coordinates", "zoom_to_region", "inspect_region"], \
        "a refused boundary was rendered after all"


def test_the_model_is_told_before_the_stall_ends_the_review(records):
    script = [*RECORDED, ("submit_findings", _submission(["v-001"]))]
    _out, msgs, _viewer = _review(script)
    # after the repeated farfield refusal (round 6) the next request carries the warning
    before_round_7 = json.dumps(msgs[6])
    assert "That round added no new usable evidence" in before_round_7
    assert "v-001" in before_round_7 and "submit_findings" in before_round_7
    assert f"{REVIEWER_NO_PROGRESS_THRESHOLD} rounds in a row" in before_round_7


def test_a_refusal_asked_for_again_and_again_still_ends_the_review(records):
    # the correction is progress ONCE; insisting on the same refused view is still a stall, so the
    # budget stays bounded - and the stall carries its own marker, which the pipeline can rerun
    farfield = RECORDED[4]
    out, _msgs, _viewer = _review([farfield] * 10)
    assert out.verdict == "" and out.api_failure == "reviewer_stalled"
    assert out.llm_rounds == 1 + REVIEWER_NO_PROGRESS_THRESHOLD
    assert records[0]["exit"] == "no_progress"


def test_a_new_refusal_is_corrected_not_counted(records):
    # three DIFFERENT refused boundaries in a row are three corrections, not a stall
    script = [
        ("toggle_patch", {"patch_name": "ground", "visible": True}),
        ("go_to_coordinates", {"x": 0, "y": 0, "z": 0, "span": 2, "patch_name": "farfield"}),
        ("go_to_coordinates", {"x": 0, "y": 0, "z": 0, "span": 2, "patch_name": "inlet"}),
        ("go_to_coordinates", {"x": 0, "y": 0, "z": 0, "span": 1, "patch_name": "Windsor_body"}),
        ("submit_findings", _submission(["v-001"])),
    ]
    out, _msgs, _viewer = _review(script)
    assert out.verdict == "PASS" and out.api_failure == ""


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made


# the words the reviewer reads

def test_the_prompt_says_which_boundaries_the_viewer_can_show():
    from meshpipeline.agents.reviewer.context import patches_line
    line = patches_line(["Windsor_body", "ground", "farfield"], ["Windsor_body"])
    assert line.startswith("Patches: Windsor_body, ground, farfield")
    assert "The viewer can show: Windsor_body." in line
    assert "ground, farfield carry no review geometry" in line
    assert "Domain bbox" in line


def test_the_prompt_is_unchanged_when_every_patch_is_viewable_or_nothing_is_known():
    from meshpipeline.agents.reviewer.context import patches_line
    assert patches_line(["inlet", "wall"], ["inlet", "wall"]) == "Patches: inlet, wall"
    assert patches_line(["inlet", "wall"], None) == "Patches: inlet, wall"


def test_a_missing_patch_inspection_names_the_tool_that_makes_one():
    # the Windsor offline replay: after framing the body with go_to_coordinates the reviewer was
    # told only "expected 1 patch inspection(s), have 0" - framing is not an inspection, toggling is
    from meshpipeline.agents.reviewer.eligibility import unmet_obligation_reasons
    from meshpipeline.contracts.evidence_ledger import TargetObligation
    from meshpipeline.contracts.review_evidence import TargetKind
    reasons = unmet_obligation_reasons(
        (TargetObligation(kind=TargetKind.PATCH, provenance="every renderable boundary patch"),),
        EvidenceLedger())
    assert len(reasons) == 1 and "toggle_patch" in reasons[0]
