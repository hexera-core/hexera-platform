# Responsibility: Prove a gmsh-shaped manifest + review context yields an ELIGIBLE submission
# path - the contract job 63ff3d42 (internal fluid domain, gmsh) died without: the review must
# be completable with exactly the evidence the gmsh session serves (iso opening + group
# isolation + measured metrics), and still refuse when the group obligations go uninspected.
from __future__ import annotations

import contextlib
import json
import re

import pytest
from tests.execution_publisher_double import RecordingExecutionPublisher

import meshpipeline.agents.reviewer.visual as visual
from meshpipeline.agents.reviewer.deterministic_evidence import collect_deterministic_evidence
from meshpipeline.agents.reviewer.eligibility import (
    AxisFinding,
    Eligibility,
    deterministic_evidence_complete,
    evaluate_eligibility,
)
from meshpipeline.agents.reviewer.unified import record_operation_evidence
from meshpipeline.agents.reviewer.visual_surface import OpeningContext
from meshpipeline.contracts import model_inference as llm_router
from meshpipeline.contracts.evidence_ledger import EvidenceLedger
from meshpipeline.contracts.review_evidence import EvidenceItem, InspectionTarget, TargetKind
from meshpipeline.engines.assurance import derive_assurance_plan
from meshpipeline.engines.registry import get_spec

# The manifest shape the gmsh finalize actually writes for an internal fluid-domain job
# (engines/gmsh/gmsh_runner.py + engines/manifest.py) - including that it publishes NO
# inspection_regions: the gmsh session serves no interior slices.
_MANIFEST = {
    "mesh_units": "m", "mesh_mode": "gmsh", "flow_topology": "internal",
    "domain": "internal CFD",
    "patch_types": {"inlet": "inlet", "outlet": "outlet", "wall": "wall"},
    "patches": {"inlet": [], "outlet": [], "wall": []},
    "patch_face_counts": {},
    "inspection_regions": [],
    "quality": {"cells": 777, "nodes": 260, "element_order": 2,
                "min_sicn": 0.34, "sicn_low_fraction": 0.0, "fatal": [],
                "groups": {"inlet": "inlet", "outlet": "outlet", "wall": "wall"}},
    "quality_criteria": {"criteria": []},
}

# what the gmsh session discovers: one surface group per contracted name + the volume group
_TARGETS = tuple(
    InspectionTarget(target_id=tid, kind=TargetKind.GROUP, label=tid, purpose="p",
                     required=False)
    for tid in ("group:2:1", "group:2:2", "group:2:3", "group:3:1"))

_PURPOSE = "internal_cfd"


def _plan():
    return derive_assurance_plan(get_spec("gmsh"), _PURPOSE)


def _ledger(plan):
    spec = get_spec("gmsh")
    ledger = EvidenceLedger()
    collect_deterministic_evidence(
        plan, ledger,
        measurements=_MANIFEST["quality"],
        criteria={c.key: c for c in spec.criteria},
        gate_status=dict.fromkeys(plan.required_gate_keys, "pass"))
    return ledger


def _mint_session_evidence(ledger, group_ids=("group:2:1", "group:2:2", "group:2:3")):
    """Mint exactly what the loop records from the real gmsh session: the opening iso view,
    then one toggle_patch isolation per group (unified.record_operation_evidence over
    GmshReviewSession-shaped EvidenceItems)."""
    ids = [ledger.add_render_view("iso", "mesh_paths.surface", "open", image_ok=True)]
    inventory = {t.target_id: t for t in _TARGETS}
    for i, tid in enumerate(group_ids, 1):
        ev = EvidenceItem(evidence_id=f"toggle-{i:03d}", seq=i, label="group shown",
                          purpose="isolate a physical group", image_ref="/shot.png",
                          covers_target=tid)
        ids.append(record_operation_evidence(ledger, ev, inventory, "toggle_patch"))
    return ids


def _usable_ids(ledger):
    out = []
    for rec in (*ledger.gates(), *ledger.metrics(), *ledger.render_views(),
                *ledger.target_inspections()):
        if rec.usable:
            out.append(rec.evidence_id)
    return tuple(out)


def _findings(plan, cite, passed=True):
    return tuple(AxisFinding(axis_key=name, finding=f"{name}: assessed",
                             evidence_ids=tuple(cite), passed=passed)
                 for name in plan.axis_names)


def _obligations():
    return get_spec("gmsh").expected_target_obligations(
        _MANIFEST, {"element_order": "2"}, _PURPOSE)


# the ledger-level contract: gmsh-producible evidence IS an eligible submission
def test_internal_cfd_gmsh_evidence_supports_an_eligible_pass():
    plan = _plan()
    ledger = _ledger(plan)
    complete, missing = deterministic_evidence_complete(plan, ledger)
    assert complete, f"finalize-measured evidence missing: {missing}"

    _mint_session_evidence(ledger)
    decision = evaluate_eligibility(plan, ledger, _findings(plan, _usable_ids(ledger)),
                                    obligations=_obligations())
    assert decision.outcome is Eligibility.PASS, decision.reasons
    assert decision.verdict is not None


def test_a_grounded_group_defect_supports_an_eligible_fail():
    plan = _plan()
    ledger = _ledger(plan)
    _mint_session_evidence(ledger)
    decision = evaluate_eligibility(plan, ledger, _findings(plan, _usable_ids(ledger),
                                                            passed=False),
                                    obligations=_obligations())
    assert decision.outcome is Eligibility.FAIL, decision.reasons


def test_uninspected_groups_still_reject_the_submission():
    # the fix aligns the demands with the producible evidence - it does NOT waive the
    # group obligations: a submission that never isolated the groups stays refused
    plan = _plan()
    ledger = _ledger(plan)
    ledger.add_render_view("iso", "mesh_paths.surface", "open", image_ok=True)
    decision = evaluate_eligibility(plan, ledger, _findings(plan, _usable_ids(ledger)),
                                    obligations=_obligations())
    assert decision.outcome is Eligibility.REJECT
    assert any("group" in r for r in decision.reasons)


# the node-level contract: the full reviewer node reaches a verdict on this job shape
_GEOMETRY_SOURCE = {
    "source_id": "11111111-1111-4111-8111-111111111111", "owner_id": "o",
    "object_key": "sources/11111111-1111-4111-8111-111111111111",
    "sha256": "1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8",
    "size_bytes": 512, "original_filename": "carve.step", "suffix_hint": ".step"}

_EVID_MARKER = re.compile(r"\[evidence:\s*([a-z]-\d{3})\]")
_EVID_CATALOG = re.compile(r"^\s+([a-z]-\d{3}):", re.MULTILINE)


class _FakeRuntime:
    def __init__(self, targets):
        self._targets = targets
        self._ids = {t.target_id for t in targets}

    async def initial_context(self):
        return OpeningContext(
            nav_context={"mesh_units": "m"}, patch_colour_legend="",
            initial_screenshot_b64="AAAA", has_geometry=True,
            patch_names=[], inspection_targets=self._targets)

    async def execute_typed(self, fn, args):
        from meshpipeline.agents.reviewer.render_runtime import NOT_VIEWER_TOOL, TypedToolResult
        if fn == "submit_findings":
            return TypedToolResult(NOT_VIEWER_TOOL, None, False)
        tid = args.get("patch_name") or ""
        img = [{"type": "text", "text": "ok"},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
        if tid in self._ids:
            ev = EvidenceItem(evidence_id="s", seq=1, label="l", purpose="p",
                              image_ref="/x.png", covers_target=tid)
            return TypedToolResult(img, ev, True)
        return TypedToolResult("no such group; nothing inspected", None, True)


class _ScriptedReviewer:
    def __init__(self, inspect, axes):
        self.inspect = list(inspect)
        self.axes = list(axes)

    def _seen_ids(self, messages):
        ids: list[str] = []
        for m in messages:
            c = m.get("content")
            if isinstance(c, str):
                ids += _EVID_MARKER.findall(c) + _EVID_CATALOG.findall(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        ids += _EVID_MARKER.findall(text) + _EVID_CATALOG.findall(text)
        return list(dict.fromkeys(ids))

    async def __call__(self, *, messages, tools, job_id, user_id):
        from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
        if self.inspect:
            tid = self.inspect.pop(0)
            call = ToolCallRequest(id=f"c-{tid}", name="toggle_patch",
                                   arguments=json.dumps({"patch_name": tid}))
            return ModelRoundResult(tool_calls=(call,), finish_reason="tool_calls")
        findings = [{"axis_key": ax, "finding": f"{ax}: assessed",
                     "evidence_ids": self._seen_ids(messages), "passed": True}
                    for ax in self.axes]
        call = ToolCallRequest(id="c-submit", name="submit_findings", arguments=json.dumps(
            {"axis_findings": findings, "rebuild_required": False, "reasoning": "done"}))
        return ModelRoundResult(tool_calls=(call,), finish_reason="tool_calls")


def _drive(reviewer, monkeypatch, tmp_path):
    import asyncio

    @contextlib.asynccontextmanager
    async def _cm(spec, ctx):
        yield _FakeRuntime(_TARGETS)

    monkeypatch.setattr(visual, "open_runtime", _cm)
    monkeypatch.setattr(llm_router, "call_reviewer_with_tools", reviewer)
    monkeypatch.setattr(visual, "save_review_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(visual, "execution_publisher",
                        lambda *a, **k: RecordingExecutionPublisher("e2e", "reviewer"))

    class _TL:
        def __init__(self, *a, **k): pass
        def log(self, *a, **k): pass
    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _TL)

    state = {
        "job_id": "e2e", "engine": "gmsh", "purpose": _PURPOSE,
        "openfoam_workspace": str(tmp_path), "mesh_manifest": dict(_MANIFEST),
        "engine_params": {"element_order": "2"}, "retry_count": 0,
        "geometry_source": _GEOMETRY_SOURCE,
        "request_txt": "mesh the fluid domain", "review_brief_txt": "check it",
        "user_id": "u", "executor_success": True,
    }
    return asyncio.run(visual.node_reviewer(state))


def test_internal_cfd_gmsh_review_reaches_a_pass_verdict(monkeypatch, tmp_path):
    plan = _plan()
    reviewer = _ScriptedReviewer(inspect=[t.target_id for t in _TARGETS[:3]],
                                 axes=list(plan.axis_names))
    out = _drive(reviewer, monkeypatch, tmp_path)
    assert out.get("reviewer_verdict") == "PASS", out
    assert not out.get("api_failure"), out
    assert out["reviewer_axis_findings"] and all(
        f["evidence_ids"] for f in out["reviewer_axis_findings"])


def test_internal_cfd_gmsh_review_without_group_evidence_stays_refused(monkeypatch, tmp_path):
    plan = _plan()
    reviewer = _ScriptedReviewer(inspect=[], axes=list(plan.axis_names))
    out = _drive(reviewer, monkeypatch, tmp_path)
    assert out.get("reviewer_verdict") != "PASS"
    assert out.get("api_failure")


# the review context itself must not promise the tool the gmsh runtime refuses
def test_gmsh_review_context_promises_no_interior_slices(tmp_path):
    from meshpipeline.agents.reviewer.context import build_review_prompt
    from meshpipeline.engines.quality_criteria import (
        compose_review_rubric,
        render_review_rubric,
    )
    system, context = build_review_prompt(
        manifest=dict(_MANIFEST), nav_context={"mesh_units": "m"}, workspace=tmp_path,
        step_basename="carve.step", patch_names=list(_MANIFEST["patches"]),
        patch_colour_legend="", mesh_units="m", request="r", review_brief="b",
        job_id="t", engine="gmsh", purpose=_PURPOSE)
    # no named slice regions are handed to the reviewer (the block that sent job
    # 63ff3d42 to a tool refused on every call)
    assert "INTERNAL SLICES" not in context
    assert "inspect_region" not in context
    # the composed rubric's "evidence to weigh" lines no longer name the refused tool
    rubric = render_review_rubric(compose_review_rubric("gmsh", _PURPOSE))
    assert "inspect_region" not in rubric
    # and the engine's own rationale counter-steers, in the same system prompt
    assert "no interior slice views" in system


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
