# Responsibility: Pin that every evidence obligation on the gmsh review path is producible by
# what the gmsh finalize/manifest/render surface actually serves - an axis must never demand
# evidence its engine cannot supply (job 63ff3d42: the manifest promised inspect_region slices
# the gmsh session categorically refuses, and the review died of no-progress with 0 submissions).
from __future__ import annotations

import pytest

from meshpipeline.contracts.review_evidence import (
    HardGateRequirement,
    RenderTargetRequirement,
    RenderViewRequirement,
    SpatialInspectionRequirement,
    TargetKind,
)
from meshpipeline.engines.assurance import derive_assurance_plan
from meshpipeline.engines.registry import get_spec

# every purpose gmsh serves, plus the purposeless composition
_GMSH_PURPOSES = ("", "structural", "internal_cfd", "external_cfd")

# the quality dict the gmsh driver actually writes (see engines/gmsh/driver.py)
_FINALIZE_QUALITY = {
    "cells": 777, "nodes": 260, "element_order": 2,
    "min_sicn": 0.34, "sicn_low_fraction": 0.0, "fatal": [], "size_h": 0.02,
    "bounds": [0, 0, 0, 0.3, 0.1, 0.1],
    "groups": {"inlet": "inlet", "outlet": "outlet", "wall": "wall"},
    "default_group": "free", "default_group_used": False,
}

_GMSH_MANIFEST = {
    "mesh_units": "m", "mesh_mode": "gmsh", "flow_topology": "internal",
    "patch_types": {"inlet": "inlet", "outlet": "outlet", "wall": "wall"},
    "patches": {"inlet": [], "outlet": [], "wall": []},
    "quality": _FINALIZE_QUALITY,
}


@pytest.mark.parametrize("purpose", _GMSH_PURPOSES)
def test_every_required_metric_is_one_the_gmsh_runner_measures(purpose):
    spec = get_spec("gmsh")
    plan = derive_assurance_plan(spec, purpose)
    declared = {c.key for c in spec.criteria}
    for key in plan.required_metric_keys:
        assert key in declared, (
            f"axis-required metric {key!r} is not a declared gmsh criterion - "
            "deterministic evidence for it can never exist")
        assert key in _FINALIZE_QUALITY, (
            f"axis-required metric {key!r} is not measured by the gmsh driver's quality "
            "report - the review would refuse before the first round")


@pytest.mark.parametrize("purpose", _GMSH_PURPOSES)
def test_every_required_view_is_a_gmsh_camera_preset(purpose):
    from meshpipeline.engines.gmsh.review_renderer import _CAMERA_PRESETS
    plan = derive_assurance_plan(get_spec("gmsh"), purpose)
    for ax in plan.axes:
        for req in getattr(ax, "requires", ()):
            if isinstance(req, RenderViewRequirement):
                assert req.view_id in _CAMERA_PRESETS, (
                    f"axis {ax.name!r} requires view {req.view_id!r} the gmsh session "
                    "cannot produce")


@pytest.mark.parametrize("purpose", _GMSH_PURPOSES)
def test_every_required_target_kind_is_a_declared_gmsh_kind(purpose):
    spec = get_spec("gmsh")
    plan = derive_assurance_plan(spec, purpose)
    declared_kinds = {t.kind for t in spec.inspection_targets}
    for ax in plan.axes:
        for req in getattr(ax, "requires", ()):
            if isinstance(req, (RenderTargetRequirement, SpatialInspectionRequirement)):
                assert req.kind in declared_kinds, (
                    f"axis {ax.name!r} requires {req.kind.value} inspections the gmsh "
                    "engine does not declare - no submission could ever be eligible")
    # the engine-resolved obligations obey the same bound
    for ob in spec.expected_target_obligations(_GMSH_MANIFEST, {"element_order": "2"}, purpose):
        assert ob.kind in declared_kinds, (
            f"engine obligation demands {ob.kind.value} inspections gmsh does not declare")


@pytest.mark.parametrize("purpose", _GMSH_PURPOSES)
def test_every_required_gate_is_a_declared_gmsh_gate(purpose):
    spec = get_spec("gmsh")
    plan = derive_assurance_plan(spec, purpose)
    declared = {g.key for g in spec.gates}
    for ax in plan.axes:
        for req in getattr(ax, "requires", ()):
            if isinstance(req, HardGateRequirement):
                assert req.key in declared, (
                    f"axis {ax.name!r} requires gate {req.key!r} gmsh never runs")


# the manifest may only PROMISE interior slices when the engine declares a REGION target
def _write(tmp_path, mesh_mode):
    from meshpipeline.engines.manifest import write_manifest
    ws = tmp_path / mesh_mode
    ws.mkdir()
    return write_manifest(
        ws,
        patch_types={"inlet": "inlet", "outlet": "outlet", "wall": "wall"},
        patch_entities={"inlet": [], "outlet": [], "wall": []},
        bbox=(0, 0, 0, 0.3, 0.1, 0.1),
        quality=dict(_FINALIZE_QUALITY),
        domain="internal CFD",
        body_bbox=((0, 0, 0), (0.3, 0.1, 0.1)),
        mesh_bounds=(0, 0, 0, 0.3, 0.1, 0.1),
        volume_path="mesh.inp",
        mesh_units="m",
        mesh_mode=mesh_mode,
        flow_topology="internal",
    )


def test_the_gmsh_manifest_promises_no_interior_slices(tmp_path):
    manifest = _write(tmp_path, "gmsh")
    assert manifest["inspection_regions"] == [], (
        "the gmsh manifest promises inspect_region slices its review session refuses on "
        "every call - the no-progress trap that dead-lettered job 63ff3d42")


def test_engines_that_serve_slices_keep_their_region_promises(tmp_path):
    # snappy and cfmesh declare REGION inspection targets, so their promises stand unchanged
    for engine in ("snappy", "cfmesh"):
        assert any(t.kind is TargetKind.REGION
                   for t in get_spec(engine).inspection_targets)
        manifest = _write(tmp_path, engine)
        assert manifest["inspection_regions"], (
            f"{engine} serves inspect_region and must keep publishing its regions")


def test_vmtk_manifest_promises_no_interior_slices(tmp_path):
    # vmtk declares OPENING/BRANCH/LAYER_REGION, no REGION kind - same rule, same gate
    assert not any(t.kind is TargetKind.REGION
                   for t in get_spec("vmtk").inspection_targets)
    assert _write(tmp_path, "vmtk")["inspection_regions"] == []


# composed rubric hints obey the same bound: no gmsh axis may steer the reviewer at a tool
# the gmsh runtime refuses unconditionally
@pytest.mark.parametrize("purpose", _GMSH_PURPOSES)
def test_composed_gmsh_rubric_never_hints_at_inspect_region(purpose):
    from meshpipeline.engines.quality_criteria import compose_review_rubric
    for ax in compose_review_rubric("gmsh", purpose):
        assert "inspect_region" not in (ax.evidence or ()), (
            f"axis {ax.name!r} steers the reviewer at inspect_region, which the gmsh "
            "runtime refuses on every call")


def test_snappy_rubric_keeps_its_inspect_region_hints():
    # the filter is capability-driven, never a blanket removal - snappy's axes are untouched
    from meshpipeline.engines.quality_criteria import compose_review_rubric
    axes = compose_review_rubric("snappy", "internal_cfd")
    assert any("inspect_region" in (ax.evidence or ()) for ax in axes)
