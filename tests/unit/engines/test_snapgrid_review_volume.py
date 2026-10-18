# Responsibility: Pin that a snap-grid case gives its review a volume, so the interior slices its manifest promises can be rendered.
# Boundaries: foamToVTK is faked; the obligation check runs on the engine's own declarations and the renderer's target expansion.
from __future__ import annotations

import json
from pathlib import Path

from meshpipeline.agents.reviewer.eligibility import missing_target_obligations
from meshpipeline.contracts.review_evidence import TargetKind
from meshpipeline.engines.manifest import _inspection_regions
from meshpipeline.engines.registry import get_spec
from meshpipeline.engines.snapgrid import native
from meshpipeline.engines.snapgrid.adapter import ENGINE
from meshpipeline.sandbox.render_adapter import expand_region_targets
from meshpipeline.sandbox.render_inputs import resolve_regions

# THE DEFECT (dev-areen set_top_box job d6a0ac60, 2026-10-11): the mesh was delivered with every
# check passing, but the review stopped before it began - "the mesh did not expose review targets
# this engine expects". The manifest promised interior slices (snapgrid declares REGION targets),
# the renderer offers them only over a volume, and snapgrid exported none: export_volume_vtk
# returned None and the manifest's volume was the constant/ directory, which is no VTU.


def _fake_foam(tmp: Path, write: bool):
    def run(case, command, log, *, bashrc, timeout):
        assert command.startswith("foamToVTK")
        if write:
            out = Path(case) / "VTK" / "case_0"
            out.mkdir(parents=True)
            (out / "internal.vtu").write_text('<?xml version="1.0"?><VTKFile type="UnstructuredGrid"/>')
        return 0, ""
    return run


def test_the_whole_mesh_is_exported_for_the_review(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "_run", _fake_foam(tmp_path, write=True))
    assert native.export_volume(tmp_path, bashrc="x") == "VTK/case_0/internal.vtu"


def test_no_volume_is_none_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(native, "_run", _fake_foam(tmp_path, write=False))
    assert native.export_volume(tmp_path, bashrc="x") is None


def test_the_export_runs_before_the_merged_mesh_is_removed():
    src = Path(native.__file__).read_text(encoding="utf-8")
    for fn in ("def check(", "def split_and_check("):
        body = src[src.index(fn):]
        body = body[:body.index("\ndef ", 10)] if "\ndef " in body[10:] else body
        assert body.index("export_volume(") < body.index('shutil.rmtree(case / "constant" / "polyMesh"'), fn


def test_the_adapter_and_the_report_name_the_same_volume(tmp_path):
    vtu = tmp_path / "VTK" / "case_0" / "internal.vtu"
    vtu.parent.mkdir(parents=True)
    vtu.write_text("x")
    (tmp_path / "snapgrid_report.json").write_text(json.dumps({"native": {"volume_vtu": "VTK/case_0/internal.vtu"}}))
    assert native.review_volume(tmp_path) == "VTK/case_0/internal.vtu"
    assert ENGINE().export_volume_vtk(tmp_path) == "VTK/case_0/internal.vtu"


def test_with_a_volume_the_promised_slices_are_reviewable():
    # the manifest's promise (write_manifest publishes slices for an engine declaring REGION)
    regions = _inspection_regions(((0.0, 0.0, 0.0), (0.18, 0.15, 0.08)))
    manifest = {"inspection_regions": regions}
    obligations = get_spec("snapgrid").expected_target_obligations(manifest, {}, "conjugate_heat_transfer")
    region_rule = next(o for o in obligations if o.kind is TargetKind.REGION)
    found = {t.target_id for t in expand_region_targets(resolve_regions(manifest), required=True,
                                                         purpose="p", volume_available=True)}
    assert not missing_target_obligations((region_rule,), {TargetKind.REGION: found})
    # ...and without one they are not - the stop the job hit
    none = {t.target_id for t in expand_region_targets(resolve_regions(manifest), required=True,
                                                       purpose="p", volume_available=False)}
    assert missing_target_obligations((region_rule,), {TargetKind.REGION: none})
