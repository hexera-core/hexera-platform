# Responsibility: Pin that a review demands only the evidence the run actually produced.
# Boundaries: real manifest writer, real node_reviewer, real review session and adapter; only the VTK backend and the model are faked.
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from tests.execution_publisher_double import RecordingExecutionPublisher
from tests.unit._render_fakes import FakeBackend

import meshpipeline.agents.reviewer.visual as visual
from meshpipeline.agents.reviewer.deterministic_evidence import collect_deterministic_evidence
from meshpipeline.agents.reviewer.eligibility import (
    AxisFinding,
    Eligibility,
    advisory_gaps,
    deterministic_evidence_complete,
    evaluate_eligibility,
)
from meshpipeline.agents.reviewer.unified import _evidence_catalog
from meshpipeline.contracts import model_inference as llm_router
from meshpipeline.contracts.evidence_ledger import (
    EvidenceLedger,
    EvidenceStatus,
    InspectionTargetRef,
)
from meshpipeline.contracts.review_evidence import TargetKind
from meshpipeline.engines.assurance import derive_assurance_plan
from meshpipeline.engines.manifest import write_manifest
from meshpipeline.engines.registry import engine_names, get_spec

# THE DEFECT (path audit, 2026-10-19): the review demanded evidence the engine never produced.
# A run that published no volume (multi-region wrote its constant/ DIRECTORY as the volume;
# snap-grid fell back to it when no VTU was exported; snappy/cfMesh when foamToVTK failed) still
# had interior slices promised and required, the session could not offer them without a volume,
# and the review ended EXPECTED_TARGET_MISSING -> reviewer_evidence_missing before the model was
# asked. Likewise an ADVISORY metric the run did not measure was held as missing required
# evidence. Either way a validated mesh was delivered as "review did not finish".

_REGION_ENGINES = [n for n in engine_names()
                   if any(t.kind is TargetKind.REGION for t in get_spec(n).inspection_targets)]

_GEOMETRY_SOURCE = {'source_id': '11111111-1111-4111-8111-111111111111', 'owner_id': 'o',
                    'object_key': 'sources/11111111-1111-4111-8111-111111111111',
                    'sha256': '1cf0557a718c367ab1cb8e34e7a86a191fbc5f1ff9f1b64f4466c8d2784ba4b8',
                    'size_bytes': 512, 'original_filename': 'part.step', 'suffix_hint': '.step'}


def _manifest(ws: Path, engine: str, volume_path):
    (ws / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    return write_manifest(
        ws, patch_types={"inlet": "patch", "outlet": "patch", "wall": "wall"},
        patch_entities={}, bbox=(0, 0, 0, 1, 1, 1),
        quality={"cells": 50_000, "max_non_ortho": 41.0, "fatal": [], "skew_fraction": 0.0,
                 "interface_ok": True},
        body_bbox=((0, 0, 0), (1, 1, 1)), volume_path=volume_path,
        mesh_units="m", mesh_mode=engine)


def _vtu(ws: Path) -> str:
    p = ws / "VTK" / "case_0" / "internal.vtu"
    p.parent.mkdir(parents=True)
    p.write_bytes(b'<?xml version="1.0"?>\n<VTKFile type="UnstructuredGrid"></VTKFile>\n')
    return str(p.resolve())


def _region_obligations(engine, manifest):
    return [o for o in get_spec(engine).expected_target_obligations(manifest, {}, "")
            if o.kind is TargetKind.REGION]


def test_there_are_region_engines_to_check():
    assert len(_REGION_ENGINES) >= 4


@pytest.mark.parametrize("engine", _REGION_ENGINES)
@pytest.mark.parametrize("volume", ["none", "directory"])
def test_no_volume_file_means_no_slice_is_promised_or_owed(tmp_path, engine, volume):
    (tmp_path / "constant").mkdir()
    raw = None if volume == "none" else str((tmp_path / "constant").resolve())
    m = _manifest(tmp_path, engine, raw)
    assert m["mesh_paths"]["volume"] is None
    assert m["inspection_regions"] == []
    assert _region_obligations(engine, m) == []


@pytest.mark.parametrize("engine", _REGION_ENGINES)
def test_a_volume_file_keeps_the_slices_owed(tmp_path, engine):
    m = _manifest(tmp_path, engine, _vtu(tmp_path))
    assert m["inspection_regions"]
    obs = _region_obligations(engine, m)
    assert obs and obs[0].exact_ids == tuple(f"region:{r['name']}"
                                             for r in m["inspection_regions"])


# the whole path: a multi-region run with no volume file reaches the model and gets a verdict
class _SceneBackend(FakeBackend):
    def scene_facts(self):
        return {"has_geometry": True,
                "bounds": {"xmin": 0.0, "xmax": 1.0, "ymin": 0.0, "ymax": 1.0,
                           "zmin": 0.0, "zmax": 1.0},
                "mesh_units": "m", "pan_step_mm": 10.0, "zoom_step": 1.5,
                "patch_legend": [(p, "#336699") for p in self.patches], "patch_views": {}}


def _install_backend(monkeypatch, patches):
    import meshpipeline.sandbox.render_adapter as RA
    from meshpipeline.sandbox.render_inputs import ResolvedRenderInputs, resolve_regions

    def _build(context, artifacts, *a, **k):
        Path(context.save_dir).mkdir(parents=True, exist_ok=True)
        be = _SceneBackend(patches=patches, save_dir=Path(context.save_dir))
        return be, ResolvedRenderInputs(surface=artifacts[RA.SURFACE_KEY],
                                        volume=artifacts.get(RA.VOLUME_KEY)), \
            resolve_regions(context.manifest)

    for e in _REGION_ENGINES:
        mod = __import__(f"meshpipeline.engines.{e}.review_renderer", fromlist=["x"])
        monkeypatch.setattr(mod, "build_backend", _build)
    monkeypatch.setattr(RA, "build_backend", _build)


class _Model:
    """Toggle one patch, then pass every axis citing what is on file."""

    def __init__(self, axes):
        self.axes, self.calls = axes, 0

    async def __call__(self, *, messages, tools, job_id="", user_id="", **kw):
        from meshpipeline.contracts.model_inference import ModelRoundResult, ToolCallRequest
        self.calls += 1
        if self.calls == 1:
            call = ToolCallRequest(id="c1", name="toggle_patch",
                                   arguments=json.dumps({"patch_name": "inlet", "visible": True}))
        else:
            ids = sorted(set(re.findall(r"\b[a-z]-\d{3}\b", str(messages))))
            call = ToolCallRequest(id=f"c{self.calls}", name="submit_findings", arguments=json.dumps(
                {"rebuild_required": False, "reasoning": "ok",
                 "axis_findings": [{"axis_key": a, "finding": "assessed", "evidence_ids": ids,
                                    "passed": True} for a in self.axes]}))
        return ModelRoundResult(tool_calls=(call,), finish_reason="tool_calls")


def test_a_multiregion_run_without_a_volume_file_is_reviewed(monkeypatch, tmp_path):
    (tmp_path / "constant").mkdir()
    m = _manifest(tmp_path, "snappy_multiregion", str((tmp_path / "constant").resolve()))
    pub = RecordingExecutionPublisher("job", "reviewer")
    monkeypatch.setattr(visual, "execution_publisher", lambda *a, **k: pub)

    class _TL:
        def __init__(self, *a, **k): pass
        def log(self, *a, **k): pass
    monkeypatch.setattr("meshpipeline.capture.logger.TrainingLogger", _TL)
    _install_backend(monkeypatch, ["inlet", "outlet", "wall"])
    model = _Model(derive_assurance_plan(get_spec("snappy_multiregion"), "").axis_names)
    monkeypatch.setattr(llm_router, "call_reviewer_with_tools", model)
    state = {"job_id": "job", "engine": "snappy_multiregion", "purpose": "",
             "openfoam_workspace": str(tmp_path), "mesh_manifest": m, "engine_params": {},
             "retry_count": 0, "review_rerun_count": 0, "geometry_source": _GEOMETRY_SOURCE,
             "request_txt": "mesh it", "review_brief_txt": "check it", "user_id": "u",
             "executor_success": True}
    out = asyncio.run(visual.node_reviewer(state))
    assert model.calls >= 2
    assert not out.get("api_failure"), out.get("api_failure")
    assert out.get("reviewer_verdict") == "PASS"


# an advisory metric the run did not measure is a stated gap, not missing evidence
def _ledger(engine, quality):
    spec = get_spec(engine)
    plan = derive_assurance_plan(spec, "")
    ledger = EvidenceLedger()
    collect_deterministic_evidence(plan, ledger, measurements=quality,
                                   criteria={c.key: c for c in spec.criteria},
                                   gate_status=dict.fromkeys(plan.required_gate_keys, "pass"))
    return plan, ledger


def _advisory_required_metrics():
    out = []
    for n in engine_names():
        spec = get_spec(n)
        crit = {c.key: c for c in spec.criteria}
        for key in sorted(derive_assurance_plan(spec, "").required_metric_keys):
            if key in crit and not crit[key].gating:
                out.append((n, key))
    return out


def test_there_is_an_advisory_metric_some_axis_requires():
    assert ("snapgrid", "resolution_short") in _advisory_required_metrics()


@pytest.mark.parametrize(("engine", "key"), _advisory_required_metrics())
def test_an_unmeasured_advisory_metric_does_not_stop_the_review(engine, key):
    plan, ledger = _ledger(engine, {"cells": 1000})               # the audit did not run
    ok, missing = deterministic_evidence_complete(plan, ledger)
    assert f"metric:{key}" not in missing
    assert key in advisory_gaps(ledger)
    assert key in _evidence_catalog(ledger)          # the reviewer is told it was not measured


def test_snapgrid_without_its_thermal_audit_still_has_complete_evidence():
    # the audit's exact case: the thermal audit raised, so resolution_short is absent
    plan, ledger = _ledger("snapgrid", {"max_non_ortho": 0.0, "cells": 1000})
    assert deterministic_evidence_complete(plan, ledger) == (True, ())
    assert advisory_gaps(ledger) == {"resolution_short"}


def test_an_unmeasured_gating_metric_is_still_missing():
    from types import SimpleNamespace
    plan = SimpleNamespace(axes=(), required_gate_keys=frozenset(), required_targets=(),
                           required_metric_keys=frozenset({"gated", "advisory"}))
    ledger = EvidenceLedger()
    ledger.add_metric("gated", None, None, EvidenceStatus.UNAVAILABLE, "not measured", "criteria",
                      gating=True)
    ledger.add_metric("advisory", None, None, EvidenceStatus.UNAVAILABLE, "not measured",
                      "criteria", gating=False)
    assert deterministic_evidence_complete(plan, ledger) == (False, ("metric:gated",))
    assert advisory_gaps(ledger) == {"advisory"}


def test_an_axis_needing_only_the_gap_is_judged_on_the_rest():
    plan, ledger = _ledger("snapgrid", {"max_non_ortho": 0.0, "cells": 1000})
    patch = ledger.add_target_inspection(InspectionTargetRef(TargetKind.PATCH, "patch:inlet"),
                                         operation="toggle_patch", image_ok=True, covered=True)
    on_file = [r.evidence_id for r in ledger.gates()] + [
        m.evidence_id for m in ledger.metrics() if m.usable] + [patch]
    findings = tuple(AxisFinding(axis_key=a, finding="assessed", evidence_ids=tuple(on_file))
                     for a in plan.axis_names)
    obligations = get_spec("snapgrid").expected_target_obligations(
        {"mesh_paths": {"volume": None}}, {}, "")
    d = evaluate_eligibility(plan, ledger, findings, obligations=obligations)
    assert d.outcome is Eligibility.PASS, d.reasons
    gap = next(m for m in ledger.metrics() if m.metric_key == "resolution_short")
    assert gap.status is EvidenceStatus.UNAVAILABLE and not gap.gating
