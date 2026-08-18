# Responsibility: Verify every engine declares its own review renderer, artifacts and typed inspection targets.
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from meshpipeline.contracts.review_evidence import (
    ArtifactFormat,
    InspectionTarget,
    RenderArtifactRequirement,
    TargetKind,
)
from meshpipeline.engines.registry import ENGINE_CATALOG

ENGINES = sorted(ENGINE_CATALOG)

# The declared coverage floor: these three engines require exactly patches + internal slices,
# and the other two require nothing. Moving ownership of the floor must not move the policy.
CURRENT_VISUAL = {"snappy", "cfmesh", "snappy_multiregion"}
CURRENT_METRIC = {"gmsh", "vmtk"}


@pytest.mark.parametrize("name", ENGINES)
def test_every_engine_declares_a_review_renderer(name):
    assert ENGINE_CATALOG[name].renders_for_review, (
        f"{name} declares no review renderer - capability must be explicit")


@pytest.mark.parametrize("name", ENGINES)
def test_the_renderer_is_owned_by_the_engine_bundle(name):
    renderer = ENGINE_CATALOG[name].review_renderer
    module = inspect.getmodule(type(renderer))
    assert module is not None
    path = Path(module.__file__ or "")
    assert path.parent.name == name, (
        f"{name}'s renderer lives in {path.parent.name}/, not its own bundle")


@pytest.mark.parametrize("name", ENGINES)
def test_renderer_capability_is_declared_not_a_policy_flag(name):
    spec = ENGINE_CATALOG[name]
    assert not hasattr(spec, "visual_review"), f"{name}: visual_review must be gone"
    assert spec.renders_for_review is True, f"{name}: must declare a renderer capability"


# artifacts: declared by manifest key, never a path
@pytest.mark.parametrize("name", ENGINES)
def test_every_engine_declares_its_render_artifacts(name):
    assert ENGINE_CATALOG[name].render_artifacts, f"{name} declares no render artifacts"


@pytest.mark.parametrize("name", ENGINES)
def test_artifacts_are_manifest_keys_not_workspace_paths(name):
    for art in ENGINE_CATALOG[name].render_artifacts:
        assert isinstance(art, RenderArtifactRequirement)
        key = art.artifact_key
        assert not key.startswith("/"), f"{name}: {key!r} is an absolute path"
        assert ".." not in key, f"{name}: {key!r} contains traversal"
        assert "\\" not in key and not key.startswith("~"), f"{name}: {key!r} looks like a path"
        assert art.allowed_formats, f"{name}: {key!r} declares no permitted format"
        for fmt in art.allowed_formats:
            assert isinstance(fmt, ArtifactFormat)
        assert art.purpose.strip(), f"{name}: {key!r} declares no purpose"


@pytest.mark.parametrize("name", ENGINES)
def test_every_engine_requires_at_least_one_artifact_to_render(name):
    assert any(a.required for a in ENGINE_CATALOG[name].render_artifacts), (
        f"{name} declares no REQUIRED render artifact")


# inspection targets
@pytest.mark.parametrize("name", ENGINES)
def test_inspection_targets_are_typed_and_purposeful(name):
    for t in ENGINE_CATALOG[name].inspection_targets:
        assert isinstance(t, InspectionTarget)
        assert isinstance(t.kind, TargetKind)
        assert t.target_id.strip() and t.label.strip()
        assert t.purpose.strip(), (
            f"{name}: target {t.target_id!r} has no purpose - a target nobody can justify is "
            "coverage for its own sake")


@pytest.mark.parametrize("name", sorted(CURRENT_VISUAL))
def test_the_current_visual_engines_keep_exactly_their_present_floor(name):
    required = {t.target_id for t in ENGINE_CATALOG[name].inspection_targets if t.required}
    assert required == {"patch:*", "region:*"}, (
        f"{name}: the required floor changed to {sorted(required)}")


@pytest.mark.parametrize("name", sorted(CURRENT_METRIC))
def test_the_metric_engines_declare_latent_capability_with_nothing_required(name):
    spec = ENGINE_CATALOG[name]
    assert spec.renders_for_review is True
    required = [t.target_id for t in spec.inspection_targets if t.required]
    assert required == [], f"{name} requires {required} - that activates visual review early"
    assert spec.inspection_targets, f"{name} records no latent targets"


def test_vmtk_records_the_defects_its_rubric_does_not_cover():
    kinds = {t.kind for t in ENGINE_CATALOG["vmtk"].inspection_targets}
    assert {TargetKind.OPENING, TargetKind.BRANCH, TargetKind.LAYER_REGION} <= kinds


def test_vmtk_declares_the_artifacts_that_make_those_defects_inspectable():
    keys = {a.artifact_key for a in ENGINE_CATALOG["vmtk"].render_artifacts}
    assert "mesh_paths.lumen" in keys and "mesh_paths.centerlines" in keys


# engine-name neutrality
def test_no_central_renderer_roster_exists_outside_the_specs():
    from meshpipeline.contracts import review_evidence
    from meshpipeline.engines import base

    for mod in (review_evidence, base):
        src = inspect.getsource(mod).lower()
        for engine in ENGINES:
            assert f'"{engine}"' not in src, f"{mod.__name__} names the engine {engine!r}"


@pytest.mark.parametrize("name", ENGINES)
def test_a_renderer_declares_the_same_artifacts_as_its_spec(name):
    spec = ENGINE_CATALOG[name]
    assert tuple(spec.review_renderer.required_artifacts()) == tuple(spec.render_artifacts)
