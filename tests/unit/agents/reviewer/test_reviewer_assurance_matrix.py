# Responsibility: Verify the pass, fail and non-verdict outcomes hold across every engine's own evidence obligations.
from __future__ import annotations

import contextlib
import re

import pytest
from tests.execution_publisher_double import RecordingExecutionPublisher

import meshpipeline.agents.reviewer.visual as visual
from meshpipeline.agents.reviewer.visual_surface import OpeningContext
from meshpipeline.contracts import model_inference as llm_router
from meshpipeline.contracts.review_evidence import EvidenceItem, InspectionTarget, TargetKind
from meshpipeline.engines.registry import ENGINE_CATALOG

# The approved upload these records describe - identity, never a workspace path.
_GEOMETRY_SOURCE = {'source_id': '11111111-1111-4111-8111-111111111111', 'owner_id': 'o', 'object_key': 'sources/11111111-1111-4111-8111-111111111111', 'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8', 'size_bytes': 512, 'original_filename': 'wing.step', 'suffix_hint': '.step'}


# Collect ids ONLY from real evidence announcements: an interactive `[evidence: t-003]` marker or a
# catalog line `  g-001: gate ...`. (A naive \b id \b would also scrape prose examples.)
_EVID_MARKER = re.compile(r"\[evidence:\s*([a-z]-\d{3})\]")
_EVID_CATALOG = re.compile(r"^\s+([a-z]-\d{3}):", re.MULTILINE)


def _scan_ids(text: str) -> list[str]:
    return _EVID_MARKER.findall(text) + _EVID_CATALOG.findall(text)


# fake runtime: real OpeningContext + typed results from covers_target
class _FakeRuntime:
    def __init__(self, targets: tuple[InspectionTarget, ...]):
        self._targets = targets
        self._ids = {t.target_id for t in targets}
        self._kind = {t.target_id: t.kind for t in targets}

    async def initial_context(self) -> OpeningContext:
        return OpeningContext(
            nav_context={"mesh_units": "m"}, patch_colour_legend="",
            initial_screenshot_b64="AAAA", has_geometry=True,
            patch_names=[t.target_id for t in self._targets if t.kind is TargetKind.PATCH],
            inspection_targets=self._targets)

    async def execute_typed(self, fn, args):
        from meshpipeline.agents.reviewer.render_runtime import NOT_VIEWER_TOOL, TypedToolResult
        if fn == "submit_findings":
            return TypedToolResult(NOT_VIEWER_TOOL, None, False)
        tid = args.get("target_id") or args.get("patch_name") or (
            f"region:{args['region_name']}" if args.get("region_name") else "")
        img = [{"type": "text", "text": "ok"},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]
        if tid in self._ids:
            ev = EvidenceItem(evidence_id="s", seq=1, label="l", purpose="p",
                              image_ref="/x.png", covers_target=tid)
            return TypedToolResult(img, ev, True)
        # a generic frame with no target provenance (or an unknown target) - no coverage
        return TypedToolResult("no such target; nothing inspected", None, True)


def _fake_open_runtime(targets):
    @contextlib.asynccontextmanager
    async def _cm(spec, ctx):
        yield _FakeRuntime(targets)
    return _cm


# scripted reviewer: inspect the given targets, then verdict + evidence-linked findings
def _tc(name, **args):
    import json

    from meshpipeline.contracts.model_inference import ToolCallRequest
    return ToolCallRequest(id=f"c-{name}", name=name, arguments=json.dumps(args))


def _resp(*calls):
    from meshpipeline.contracts.model_inference import ModelRoundResult
    return ModelRoundResult(tool_calls=tuple(calls),
                            finish_reason="tool_calls" if calls else "stop")


class _Reviewer:

    def __init__(self, *, inspect, verdict, axes, omit_axis=None, blank_axis=None,
                 no_evidence=False, wrong_only=None):
        self.inspect = list(inspect)
        self.verdict = verdict
        self.axes = list(axes)
        self.omit_axis = omit_axis
        self.blank_axis = blank_axis
        self.no_evidence = no_evidence
        self.wrong_only = wrong_only          # cite only this id for every axis (wrong-evidence case)

    def _seen_ids(self, messages):
        ids: list[str] = []
        for m in messages:
            c = m.get("content")
            if isinstance(c, str):
                ids += _scan_ids(c)
            elif isinstance(c, list):
                for part in c:
                    if isinstance(part, dict) and part.get("type") == "text":
                        ids += _scan_ids(part.get("text", ""))
        return list(dict.fromkeys(ids))

    async def __call__(self, *, messages, tools, job_id, user_id):
        if self.inspect:
            tid = self.inspect.pop(0)
            return _resp(_tc("toggle_patch", patch_name=tid))
        ids = self._seen_ids(messages)
        findings = []
        for ax in self.axes:
            if ax == self.omit_axis:
                continue
            text = "" if ax == self.blank_axis else f"{ax}: assessed"
            cite = [] if self.no_evidence else (self.wrong_only or ids)
            # ONE submission carries the per-axis judgement; there is no separate verdict call.
            findings.append({"axis_key": ax, "finding": text, "evidence_ids": cite,
                             "passed": self.verdict != "FAIL"})
        return _resp(_tc("submit_findings", rebuild_required=False, reasoning="done",
                         axis_findings=findings))


# the driver: real node_reviewer, fake runtime + inference
def _targets(*specs):
    out = []
    for kind, tid in specs:
        out.append(InspectionTarget(target_id=tid, kind=kind, label=tid, purpose="p",
                                    required=False))
    return tuple(out)


def _drive(engine, *, manifest, engine_params, targets, reviewer, monkeypatch, tmp_path):
    import asyncio

    monkeypatch.setattr(visual, "open_runtime", _fake_open_runtime(targets))
    monkeypatch.setattr(llm_router, "call_reviewer_with_tools", reviewer)
    # side effects off - we assert on the returned state dict
    monkeypatch.setattr(visual, "save_review_artifacts", lambda *a, **k: None)

    # The committed double: the declared async port spelled out, and NOTHING else. A double
    # that answers to any attribute cannot fail when production asks for one that does not
    # exist - which is how the reviewer's obsolete `hasattr(publish, "warn")` guard passed
    # here while crashing every real run.
    monkeypatch.setattr(visual, "execution_publisher",
                        lambda *a, **k: RecordingExecutionPublisher("e2e", "reviewer"))

    class _TL:
        def __init__(self, *a, **k): pass
        def log(self, *a, **k): pass
    # the node writes no TrainingLogger event of its own since the canonical-loop cutover;
    # the canonical run record goes through the shared sink, so silence it there
    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _TL)

    state = {
        "job_id": "e2e", "engine": engine, "purpose": "",
        "openfoam_workspace": str(tmp_path), "mesh_manifest": manifest,
        "engine_params": engine_params, "retry_count": 0, "geometry_source": _GEOMETRY_SOURCE,
        "request_txt": "mesh it", "review_brief_txt": "check it", "user_id": "u",
        # the reviewer only ever judges an executor-validated mesh defense-in-depth)
        "executor_success": True,
    }
    return asyncio.run(visual.node_reviewer(state))


def _axes(engine):
    from meshpipeline.engines.assurance import derive_assurance_plan
    return list(derive_assurance_plan(ENGINE_CATALOG[engine], "").axis_names)


# per-engine fixtures: manifest with passing metrics + the discovered targets
_GMSH_MANIFEST = {"mesh_units": "m", "patch_types": {"fixed": 1, "load": 1},
                  "quality": {"sicn_low_fraction": 0.0, "min_sicn": 0.6}, "patches": {}}
_GMSH_TARGETS = _targets((TargetKind.GROUP, "group:2:1"), (TargetKind.GROUP, "group:2:2"))

_VMTK_MANIFEST = {"mesh_units": "m", "n_open_profiles": 2,
                  "mesh_paths": {"surface": "m.msh", "centerlines": "c.vtp"},
                  "quality": {"layer_coverage": 0.85, "min_quality": 0.5, "cells": 5000}}
_VMTK_PARAMS = {"boundary_layers": 2}
_VMTK_TARGETS = _targets((TargetKind.OPENING, "opening:0"), (TargetKind.OPENING, "opening:1"),
                         (TargetKind.LAYER_REGION, "layer_region:wall"),
                         (TargetKind.BRANCH, "branch:0"))

_SNAPPY_MANIFEST = {"mesh_units": "m", "inspection_regions": [{"name": "midspan"}],
                    "patches": {"inlet": 1, "wall": 1},
                    "quality": {"max_non_ortho": 10.0, "layer_coverage": 0.7, "skew_fraction": 0.0}}
_SNAPPY_TARGETS = _targets((TargetKind.PATCH, "patch:inlet"), (TargetKind.PATCH, "patch:wall"),
                           (TargetKind.REGION, "region:midspan"))

_CFMESH_MANIFEST = {"mesh_units": "m", "patches": {"inlet": 1, "wall": 1},
                    "quality": {"max_non_ortho": 10.0}}
_CFMESH_TARGETS = _targets((TargetKind.PATCH, "patch:inlet"), (TargetKind.PATCH, "patch:wall"))

_MR_MANIFEST = {"mesh_units": "m", "regions": [{"name": "fluid"}, {"name": "solid"}],
                "patches": {"inlet": 1},
                "quality": {"max_non_ortho": 10.0, "layer_coverage": 0.7, "skew_fraction": 0.0}}
_MR_TARGETS = _targets((TargetKind.PATCH, "patch:inlet"),
                       (TargetKind.REGION, "region:fluid"), (TargetKind.REGION, "region:solid"))

_ENGINE = {
    "gmsh": (_GMSH_MANIFEST, {}, _GMSH_TARGETS),
    "vmtk": (_VMTK_MANIFEST, _VMTK_PARAMS, _VMTK_TARGETS),
    "snappy": (_SNAPPY_MANIFEST, {}, _SNAPPY_TARGETS),
    "cfmesh": (_CFMESH_MANIFEST, {}, _CFMESH_TARGETS),
    "snappy_multiregion": (_MR_MANIFEST, {}, _MR_TARGETS),
}


def _inspect_ids(targets):
    return [t.target_id for t in targets]


# per-engine PASS (complete hybrid evidence)
@pytest.mark.parametrize("engine", sorted(_ENGINE))
def test_complete_hybrid_evidence_passes(engine, monkeypatch, tmp_path):
    m, ep, targets = _ENGINE[engine]
    rv = _Reviewer(inspect=_inspect_ids(targets), verdict="PASS", axes=_axes(engine))
    out = _drive(engine, manifest=m, engine_params=ep, targets=targets, reviewer=rv,
                 monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert out.get("reviewer_verdict") == "PASS", out
    # findings are evidence-linked
    assert out["reviewer_axis_findings"] and all(
        f["evidence_ids"] for f in out["reviewer_axis_findings"])


# A failing ADVISORY metric is reported, not vetoed. Every axis-required metric these engines
# declare is advisory - max_non_ortho (snappy/cfmesh/multiregion), sicn_low_fraction (gmsh),
# layer_coverage (vmtk) - and the genuinely gating criteria never reach the reviewer at all: the
# quality_floor flow gate refuses the mesh first. So a veto here could only ever be a false one, and
# it was: it destroyed a production-grade Ahmed body mesh whose own manifest said production_grade.
@pytest.mark.parametrize("engine", sorted(_ENGINE))
def test_failing_advisory_metric_does_not_block_pass(engine, monkeypatch, tmp_path):
    m, ep, targets = _ENGINE[engine]
    m = {**m, "quality": {**m["quality"]}}
    # break the engine's required metric so it FAILS its criterion
    for k in ("max_non_ortho",):
        if k in m["quality"]:
            m["quality"][k] = 1e6
    if "sicn_low_fraction" in m["quality"]:
        m["quality"]["sicn_low_fraction"] = 1.0
    if "layer_coverage" in m["quality"] and engine == "vmtk":
        m["quality"]["layer_coverage"] = -1.0
    rv = _Reviewer(inspect=_inspect_ids(targets), verdict="PASS", axes=_axes(engine))
    out = _drive(engine, manifest=m, engine_params=ep, targets=targets, reviewer=rv,
                 monkeypatch=monkeypatch, tmp_path=tmp_path)
    # the advisory bar is missed, and the mesh still reaches a verdict rather than dying
    assert out.get("reviewer_verdict") == "PASS", out
    assert not out.get("api_failure"), out


# missing required target evidence cannot PASS
@pytest.mark.parametrize("engine", sorted(_ENGINE))
def test_missing_target_inspection_cannot_pass(engine, monkeypatch, tmp_path):
    m, ep, targets = _ENGINE[engine]
    # inspect NOTHING -> required target obligations unmet -> PASS blocked
    rv = _Reviewer(inspect=[], verdict="PASS", axes=_axes(engine))
    out = _drive(engine, manifest=m, engine_params=ep, targets=targets, reviewer=rv,
                 monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert out.get("reviewer_verdict") != "PASS"
    assert out.get("api_failure")


# a grounded defect can FAIL
@pytest.mark.parametrize("engine", sorted(_ENGINE))
def test_grounded_defect_can_fail(engine, monkeypatch, tmp_path):
    m, ep, targets = _ENGINE[engine]
    # A verdict of EITHER polarity needs the engine's full evidence obligations met - the
    # difference is only which axes are marked not passed. Inspect everything, then fail.
    rv = _Reviewer(inspect=_inspect_ids(targets), verdict="FAIL", axes=_axes(engine))
    out = _drive(engine, manifest=m, engine_params=ep, targets=targets, reviewer=rv,
                 monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert out.get("reviewer_verdict") == "FAIL", out


# partial target loss (discovery short of the obligation) is non-verdict
def test_partial_vmtk_opening_loss_is_non_verdict(monkeypatch, tmp_path):
    # n_open_profiles=2 but discovery produces ONE opening -> partial loss, blocked before provider
    targets = _targets((TargetKind.OPENING, "opening:0"),
                       (TargetKind.LAYER_REGION, "layer_region:wall"),
                       (TargetKind.BRANCH, "branch:0"))
    rv = _Reviewer(inspect=[t.target_id for t in targets], verdict="PASS", axes=_axes("vmtk"))
    out = _drive("vmtk", manifest=_VMTK_MANIFEST, engine_params=_VMTK_PARAMS, targets=targets,
                 reviewer=rv, monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert out.get("reviewer_verdict") != "PASS" and out.get("api_failure")


def test_snappy_region_less_job_is_valid_without_a_fake_region_obligation(monkeypatch, tmp_path):
    # no authored regions -> no REGION obligation; inspecting the patches is enough to PASS
    m = {**_SNAPPY_MANIFEST}
    m.pop("inspection_regions")
    targets = _targets((TargetKind.PATCH, "patch:inlet"), (TargetKind.PATCH, "patch:wall"))
    rv = _Reviewer(inspect=[t.target_id for t in targets], verdict="PASS", axes=_axes("snappy"))
    out = _drive("snappy", manifest=m, engine_params={}, targets=targets, reviewer=rv,
                 monkeypatch=monkeypatch, tmp_path=tmp_path)
    assert out.get("reviewer_verdict") == "PASS", out


@pytest.mark.parametrize("engine,kinds", [
    ("gmsh", {TargetKind.GROUP}),
    ("vmtk", {TargetKind.OPENING, TargetKind.BRANCH, TargetKind.LAYER_REGION}),
])
def test_non_patch_kinds_survive_into_coverage(engine, kinds, monkeypatch, tmp_path):
    from meshpipeline.agents.reviewer.unified import record_operation_evidence
    from meshpipeline.contracts.evidence_ledger import EvidenceLedger
    _m, _ep, targets = _ENGINE[engine]
    L = EvidenceLedger()
    for t in targets:
        ev = EvidenceItem(evidence_id="s", seq=1, label="l", purpose="p", image_ref="/x.png",
                          covers_target=t.target_id)
        record_operation_evidence(L, ev, {t.target_id: t}, "toggle_patch")
    covered = {r.target.kind for r in L.target_inspections() if r.usable}
    assert kinds <= covered
    assert TargetKind.PATCH not in covered or engine != "gmsh"
    # no patch: coercion of the ids
    for r in L.target_inspections():
        assert not r.target.target_id.startswith("patch:")


# cross-engine contract

import inspect as _inspect  # noqa: E402

import meshpipeline.agents.reviewer.unified as _unified  # noqa: E402

_ENGINES = ("cfmesh", "snappy", "snappy_multiregion", "gmsh", "vmtk")


def test_every_engine_opens_its_own_renderer_through_the_enginespec():
    import meshpipeline.engines.gmsh.review_renderer as g
    import meshpipeline.engines.vmtk.review_renderer as v
    for e in _ENGINES:
        assert ENGINE_CATALOG[e].review_renderer is not None
        assert ENGINE_CATALOG[e].renders_for_review is True
    assert type(ENGINE_CATALOG["gmsh"].review_renderer) is g.GmshReviewRenderer
    assert type(ENGINE_CATALOG["vmtk"].review_renderer) is v.VmtkReviewRenderer


def test_every_engine_derives_a_render_backed_assurance_plan():
    from meshpipeline.engines.assurance import derive_assurance_plan
    for e in _ENGINES:
        p = derive_assurance_plan(ENGINE_CATALOG[e], "")
        assert p.requires_render


def test_the_shared_orchestration_imports_no_engine_bundle():
    import ast

    import meshpipeline.agents.reviewer.eligibility as _elig
    import meshpipeline.contracts.evidence_ledger as _ledger
    for mod in (_unified, _elig, _ledger):
        tree = ast.parse(_inspect.getsource(mod))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                name = getattr(node, "module", "") or ""
                assert "engines." not in name or "assurance" in name, (
                    f"{mod.__name__} imports an engine bundle: {name}")


def test_an_invalid_image_or_a_generic_frame_creates_no_target_coverage():
    from meshpipeline.agents.reviewer.unified import record_operation_evidence
    from meshpipeline.contracts.evidence_ledger import EvidenceLedger
    inv = {"opening:x": InspectionTarget("opening:x", TargetKind.OPENING, "l", "p", False)}
    L = EvidenceLedger()
    # failed render (no image) -> validation record, no coverage
    record_operation_evidence(
        L, EvidenceItem("s", 1, "l", "p", image_ref="", covers_target="opening:x"), inv, "t")
    # generic frame (image but no covers_target) -> render_view, not target coverage
    record_operation_evidence(
        L, EvidenceItem("s", 2, "l", "p", image_ref="/x.png", covers_target=""), inv, "t")
    assert not any(t.usable for t in L.target_inspections())


def test_renderer_failure_is_a_non_verdict_not_provider_down(monkeypatch, tmp_path):
    import contextlib as _c

    from meshpipeline.contracts.review_evidence import ReviewEvidenceFailure, ReviewRenderError
    from meshpipeline.errors import FailureClass, classify_api_failure

    @_c.asynccontextmanager
    async def _boom(spec, ctx):
        raise ReviewRenderError(ReviewEvidenceFailure.RENDERER_UNAVAILABLE, "renderer init failed")
        yield  # pragma: no cover

    m, ep, targets = _ENGINE["gmsh"]
    monkeypatch.setattr(visual, "open_runtime", lambda spec, ctx: _boom(spec, ctx))
    monkeypatch.setattr(llm_router, "call_reviewer_with_tools",
                        _Reviewer(inspect=[], verdict="PASS", axes=_axes("gmsh")))
    monkeypatch.setattr(visual, "save_review_artifacts", lambda *a, **k: None)

    # The committed double: the declared async port spelled out, and NOTHING else. A double
    # that answers to any attribute cannot fail when production asks for one that does not
    # exist - which is how the reviewer's obsolete `hasattr(publish, "warn")` guard passed
    # here while crashing every real run.
    monkeypatch.setattr(visual, "execution_publisher",
                        lambda *a, **k: RecordingExecutionPublisher("e2e", "reviewer"))

    class _TL:
        def __init__(self, *a, **k): pass
        def log(self, *a, **k): pass
    # the node writes no TrainingLogger event of its own since the canonical-loop cutover;
    # the canonical run record goes through the shared sink, so silence it there
    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _TL)

    import asyncio
    out = asyncio.run(visual.node_reviewer({
        "job_id": "e2e", "engine": "gmsh", "purpose": "", "openfoam_workspace": str(tmp_path),
        "mesh_manifest": m, "engine_params": ep, "retry_count": 0, "geometry_source": _GEOMETRY_SOURCE,
        "request_txt": "m", "review_brief_txt": "c", "user_id": "u", "executor_success": True}))
    assert out.get("reviewer_verdict") is None and out.get("api_failure")
    assert out["api_failure"] == "reviewer_render_unavailable"
    assert classify_api_failure(out["api_failure"]) is FailureClass.REVIEW_EVIDENCE_MISSING
    assert classify_api_failure(out["api_failure"]) is not FailureClass.PROVIDER_DOWN


@pytest.fixture(autouse=True)
def _reviewer_execution_publisher(monkeypatch):
    # the reviewer publishes through the ownership-checked port; this suite is about the review
    from tests.execution_publisher_double import install

    import meshpipeline.agents.reviewer.visual as _visual
    import meshpipeline.application.execution_publisher as _ep
    made = install(monkeypatch, _ep)
    monkeypatch.setattr(_visual, "execution_publisher", _ep.execution_publisher)
    return made
