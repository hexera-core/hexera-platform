# Responsibility: Verify each engine's review adapter opens through the sandbox and renders only declared targets.
# Boundaries: capabilities come from the loaded scene, so narration cannot become a region and no patch is dropped.
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from meshpipeline.contracts.review_evidence import (
    ArtifactFormat,
    CommandKind,
    EvidenceItem,
    RenderCommand,
    RenderContext,
    ResolvedArtifact,
    ReviewRenderError,
    SessionUpdateResult,
    TargetKind,
)
from meshpipeline.engines.registry import ENGINE_CATALOG

VISUAL = ("snappy", "cfmesh", "snappy_multiregion")
LATENT = ()  # every registered engine now exports a real review renderer (gmsh + vmtk are real)

from _render_fakes import (  # noqa: E402  (tests/unit is on sys.path via conftest)
    PATCHES,
    PNG,
    PNG_CROP,
    PNG_SLICE,
    REGIONS,
    FakeBackend,
)


def _artifacts(tmp_path, *, volume=True):
    out = {"mesh_paths.surface": ResolvedArtifact(
        "mesh_paths.surface", tmp_path / "mesh.msh", ArtifactFormat.GMSH_MSH)}
    if volume:
        out["mesh_paths.volume"] = ResolvedArtifact(
            "mesh_paths.volume", tmp_path / "internal.vtu", ArtifactFormat.VTK_VTU)
    return out


def _context(tmp_path):
    return RenderContext(
        workspace=str(tmp_path), save_dir=str(tmp_path),
        manifest={"mesh_units": "m",
                  "patch_types": {"wall": "wall"},
                  "inspection_regions": REGIONS,
                  "mesh_paths": {"surface": "/etc/passwd", "volume": "/etc/shadow"}})


@pytest.fixture
def fake_backend(monkeypatch):
    made: list[FakeBackend] = []

    def _build(context, artifacts, backend_factory=None, **kw):
        from meshpipeline.sandbox.render_adapter import SURFACE_KEY, VOLUME_KEY
        from meshpipeline.sandbox.render_inputs import ResolvedRenderInputs, resolve_regions
        surface = artifacts.get(SURFACE_KEY)
        if surface is None:
            from meshpipeline.contracts.review_evidence import ReviewEvidenceFailure
            raise ReviewRenderError(ReviewEvidenceFailure.EVIDENCE_MISSING,
                                    f"required artifact {SURFACE_KEY!r} was not resolved")
        be = FakeBackend()
        made.append(be)
        return be, ResolvedRenderInputs(surface=surface, volume=artifacts.get(VOLUME_KEY)), \
            resolve_regions(context.manifest)

    import meshpipeline.sandbox.render_adapter as RA
    for engine in VISUAL:
        mod = __import__(f"meshpipeline.engines.{engine}.review_renderer", fromlist=["x"])
        monkeypatch.setattr(mod, "build_backend", _build)
    monkeypatch.setattr(RA, "build_backend", _build)
    return made


def _open(engine, tmp_path, *, volume=True):
    return ENGINE_CATALOG[engine].review_renderer.open(
        _context(tmp_path), _artifacts(tmp_path, volume=volume))


# 1-3: the renderer is real, owned, and opens
@pytest.mark.parametrize("engine", VISUAL)
def test_the_spec_resolves_its_own_functioning_renderer(engine, fake_backend, tmp_path):
    renderer = ENGINE_CATALOG[engine].review_renderer
    module = inspect.getmodule(type(renderer))
    assert Path(module.__file__).parent.name == engine, "the renderer left its bundle"
    session = _open(engine, tmp_path)
    assert session is not None


@pytest.mark.parametrize("engine", VISUAL)
def test_the_renderer_no_longer_reports_itself_unavailable(engine, fake_backend, tmp_path):
    session = _open(engine, tmp_path)
    assert session.capabilities().operations


@pytest.mark.parametrize("engine", VISUAL)
def test_it_opens_through_the_sandbox_lifecycle(engine, fake_backend, tmp_path):
    import asyncio

    from meshpipeline.sandbox.review_session import open_review_session

    (tmp_path / "mesh.msh").write_bytes(b"$MeshFormat\n4.1 0 8\n$EndMeshFormat\n")
    ctx = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path),
                        manifest={"mesh_paths": {"surface": str(tmp_path / "mesh.msh")},
                                  "inspection_regions": REGIONS})

    async def go():
        async with open_review_session(ENGINE_CATALOG[engine], ctx) as s:
            return s.capabilities()

    assert asyncio.run(go()).targets


# 4-6: what the adapter may not do
@pytest.mark.parametrize("engine", VISUAL)
def test_the_adapter_reads_no_artifact_path_from_the_manifest(engine):
    src = Path(inspect.getsourcefile(
        type(ENGINE_CATALOG[engine].review_renderer))).read_text()
    tree = ast.parse(src)
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef)) and n.body
            and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    evaluated = {n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and id(n) not in docs}
    for banned in ("mesh_paths", "mesh_paths.surface", "mesh_paths.volume"):
        assert banned not in evaluated, f"{engine} adapter reads {banned!r}"


@pytest.mark.parametrize("engine", VISUAL)
def test_the_adapter_imports_no_reviewer_orchestration(engine):
    src = Path(inspect.getsourcefile(
        type(ENGINE_CATALOG[engine].review_renderer))).read_text()
    assert "agents.reviewer" not in src and "node_reviewer" not in src


# 7-8: opening evidence
@pytest.mark.parametrize("engine", VISUAL)
def test_it_produces_opening_evidence(engine, fake_backend, tmp_path):
    ev = _open(engine, tmp_path).opening_evidence()
    assert len(ev) == 1, "opening added views - that changes what every review starts from"
    assert ev[0].image_ref and ev[0].seq == 1 and ev[0].view_id == "iso"
    assert ev[0].artifact_key == "mesh_paths.surface"


@pytest.mark.parametrize("engine", VISUAL)
def test_opening_is_deterministic_and_rendered_once(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    a, b = s.opening_evidence(), s.opening_evidence()
    assert a == b, "opening evidence is not deterministic"
    assert sum(1 for c in fake_backend[0].calls if c[0] == "take_screenshot") == 1


# 9-14: capability expansion
@pytest.mark.parametrize("engine", VISUAL)
def test_patch_capabilities_come_from_the_loaded_scene(engine, fake_backend, tmp_path):
    caps = _open(engine, tmp_path).capabilities()
    patches = [t for t in caps.targets if t.kind is TargetKind.PATCH]
    assert [t.label for t in patches] == PATCHES, "the patch set does not match the loaded mesh"
    assert [t.target_id for t in patches] == ["patch:inlet", "patch:outlet", "patch:wall"]
    assert all(t.required for t in patches), "the spec's floor was not inherited"


@pytest.mark.parametrize("engine", VISUAL)
def test_no_patch_is_dropped_for_looking_unimportant(engine, fake_backend, tmp_path):
    caps = _open(engine, tmp_path).capabilities()
    assert {t.label for t in caps.targets if t.kind is TargetKind.PATCH} == set(PATCHES)
    assert set(caps.entities) == set(PATCHES)


@pytest.mark.parametrize("engine", VISUAL)
def test_region_capabilities_come_from_validated_declarations(engine, fake_backend, tmp_path):
    caps = _open(engine, tmp_path).capabilities()
    regions = [t for t in caps.targets if t.kind is TargetKind.REGION]
    assert {t.label for t in regions} == {"midspan", "nearwall"}
    assert {t.target_id for t in regions} == {"region:midspan", "region:nearwall"}


def test_patch_ids_that_collide_after_normalization_are_refused():
    from meshpipeline.sandbox.render_adapter import expand_patch_targets

    out = expand_patch_targets(["wall", "wall", "  ", ""], required=True, purpose="p")
    assert [t.target_id for t in out] == ["patch:wall"], (
        "a collision merged two patches into one target - one toggle would cover both")


def test_the_patch_count_is_bounded():
    from meshpipeline.sandbox.render_adapter import MAX_PATCHES, expand_patch_targets

    out = expand_patch_targets([f"p{i}" for i in range(MAX_PATCHES + 50)],
                               required=True, purpose="p")
    assert len(out) == MAX_PATCHES


@pytest.mark.parametrize("engine", VISUAL)
def test_capabilities_are_deterministic(engine, fake_backend, tmp_path):
    a = _open(engine, tmp_path).capabilities()
    b = _open(engine, tmp_path).capabilities()
    assert a == b


# 11-13: unknown inputs are refused, not absorbed
@pytest.mark.parametrize("engine", VISUAL)
def test_an_unknown_patch_is_refused_and_renders_nothing(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="nonexistent",
                                visible=False))
    assert isinstance(r, EvidenceItem)
    assert r.image_ref == "" and r.covers_target == ""
    assert any("unknown patch" in d for d in r.diagnostics)
    assert not any(c[0] == "toggle_patch" for c in fake_backend[0].calls)


@pytest.mark.parametrize("engine", VISUAL)
def test_an_unknown_region_is_refused_and_renders_nothing(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET, target_id="region:invented"))
    assert r.image_ref == "" and r.covers_target == ""
    assert any("unknown inspection region" in d for d in r.diagnostics)
    assert not any(c[0] == "inspect_region" for c in fake_backend[0].calls)


@pytest.mark.parametrize("engine", VISUAL)
def test_arbitrary_narration_cannot_become_a_region(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    for narration in ("the boundary layer near the trailing edge", "", "   ", "region:"):
        r = s.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET, target_id=narration))
        assert r.image_ref == "", f"{narration!r} rendered"
        assert r.covers_target == ""


def test_nan_region_coordinates_remain_refused():
    from meshpipeline.sandbox.render_inputs import resolve_regions

    got = resolve_regions({"inspection_regions": [
        {"name": "ok", "normal": [0, 1, 0]},
        {"name": "nan_normal", "normal": [0, float("nan"), 0]},
        {"name": "inf_origin", "normal": [0, 1, 0], "origin": [float("inf"), 0, 0]},
    ]})
    assert set(got) == {"ok"}


# 15-18: the complete viewer-operation mapping
@pytest.mark.parametrize("engine", VISUAL)
def test_all_nine_rendering_operations_map_and_render(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    cmds = [
        ("set_camera_preset", RenderCommand(kind=CommandKind.SELECT_VIEW, view_id="front")),
        ("move_camera", RenderCommand(kind=CommandKind.CAMERA_OP, camera="up", amount=5.0)),
        ("rotate_camera", RenderCommand(kind=CommandKind.CAMERA_OP, camera="yaw", amount=30.0)),
        ("zoom", RenderCommand(kind=CommandKind.CAMERA_OP, camera="zoom", amount=2.0)),
        ("go_to_coordinates", RenderCommand(kind=CommandKind.GO_TO_COORDINATES,
                                            coordinates=(1.0, 2.0, 3.0), span=10.0)),
        ("zoom_to_region", RenderCommand(kind=CommandKind.APPLY_CLIP,
                                         coordinates=(0.5, 0.5, 0.0), span=0.25)),
        ("inspect_region", RenderCommand(kind=CommandKind.INSPECT_TARGET,
                                         target_id="region:midspan")),
        ("toggle_patch", RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="wall",
                                       visible=False)),
        ("reset_view", RenderCommand(kind=CommandKind.CAMERA_OP, camera="reset")),
    ]
    for backend_call, cmd in cmds:
        r = s.execute(cmd)
        assert isinstance(r, EvidenceItem), f"{backend_call} did not return evidence"
        assert r.image_ref, f"{backend_call} produced no image"
        assert backend_call in {c[0] for c in fake_backend[0].calls}, (
            f"{backend_call} never reached the backend")
    assert len(cmds) == 9


@pytest.mark.parametrize("engine", VISUAL)
def test_set_navigation_defaults_maps_to_configuration(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION,
                                pan_step_mm=25.0, zoom_step=2.0))
    assert isinstance(r, SessionUpdateResult) and not isinstance(r, EvidenceItem)


@pytest.mark.parametrize("engine", VISUAL)
def test_the_configuration_result_text_is_byte_identical(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION,
                                pan_step_mm=25.0, zoom_step=2.0))
    assert r.message == "Navigation defaults updated: pan_step=25 mm, zoom_step=2.00×."


@pytest.mark.parametrize("engine", VISUAL)
def test_configuration_renders_nothing_and_covers_nothing(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    before = len([c for c in fake_backend[0].calls if c[0] == "take_screenshot"])
    r = s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION, pan_step_mm=5.0))
    after = len([c for c in fake_backend[0].calls if c[0] == "take_screenshot"])
    assert after == before, "a configure rendered"
    assert not hasattr(r, "image_ref") and not hasattr(r, "covers_target")


@pytest.mark.parametrize("engine", VISUAL)
def test_configuration_affects_subsequent_navigation(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION, pan_step_mm=42.0))
    assert fake_backend[0].pan_step == 42.0
    s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION, zoom_step=3.0))
    assert fake_backend[0].zoom_step == 3.0 and fake_backend[0].pan_step == 42.0


# 19-23: coverage honesty
@pytest.mark.parametrize("engine", VISUAL)
def test_patch_coverage_advances_only_on_rendered_evidence(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="wall", visible=False))
    assert r.covers_target == "patch:wall" and r.patches == ("wall",) and r.image_ref


@pytest.mark.parametrize("engine", VISUAL)
def test_region_coverage_advances_only_on_rendered_evidence(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET, target_id="region:midspan"))
    assert r.covers_target == "region:midspan" and r.regions == ("midspan",) and r.image_ref
    assert r.artifact_key == "mesh_paths.volume"


@pytest.mark.parametrize("engine", VISUAL)
def test_a_render_that_produces_no_image_covers_nothing(engine, monkeypatch, tmp_path,
                                                        fake_backend):
    s = _open(engine, tmp_path)
    fake_backend[0]._shot = False         # the renderer starts producing nothing
    r = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="wall", visible=False))
    assert r.image_ref == ""
    assert r.covers_target == "", "a blank render claimed coverage"


@pytest.mark.parametrize("engine", VISUAL)
def test_evidence_provenance_is_a_manifest_key_not_a_path(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.SELECT_VIEW, view_id="top"))
    assert r.artifact_key == "mesh_paths.surface"
    assert "/" not in r.artifact_key.replace("mesh_paths.surface", "")


@pytest.mark.parametrize("engine", VISUAL)
def test_no_axis_is_bound_before_commit_four(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.SELECT_VIEW, view_id="top"))
    assert r.axis_ids == ()
    assert s.opening_evidence()[0].axis_ids == ()


# 24-26: optional volume
@pytest.mark.parametrize("engine", VISUAL)
def test_a_missing_volume_still_opens_a_usable_surface_session(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path, volume=False)
    assert s.opening_evidence()[0].image_ref
    r = s.execute(RenderCommand(kind=CommandKind.SELECT_VIEW, view_id="front"))
    assert r.image_ref, "surface navigation broke without a volume"


@pytest.mark.parametrize("engine", VISUAL)
def test_a_missing_volume_removes_volume_dependent_capability(engine, fake_backend, tmp_path):
    caps = _open(engine, tmp_path, volume=False).capabilities()
    assert CommandKind.INSPECT_TARGET not in caps.operations
    assert not [t for t in caps.targets if t.kind is TargetKind.REGION]
    # ...and the surface capabilities survive.
    assert CommandKind.TOGGLE_ENTITY in caps.operations
    assert [t for t in caps.targets if t.kind is TargetKind.PATCH]


@pytest.mark.parametrize("engine", VISUAL)
def test_a_volume_command_without_a_volume_fails_rather_than_pretending(engine, fake_backend,
                                                                       tmp_path):
    s = _open(engine, tmp_path, volume=False)
    r = s.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET, target_id="region:midspan"))
    assert r.image_ref == "" and r.covers_target == ""


@pytest.mark.parametrize("engine", VISUAL)
def test_a_valid_volume_preserves_internal_inspection(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path, volume=True)
    assert CommandKind.INSPECT_TARGET in s.capabilities().operations
    r = s.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET, target_id="region:midspan"))
    assert r.image_ref.endswith(".png") and Path(r.image_ref).is_file()
    assert ("inspect_region", "midspan") in fake_backend[0].calls


# 27-30: lifecycle
@pytest.mark.parametrize("engine", VISUAL)
def test_state_persists_across_commands(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="wall", visible=False))
    s.execute(RenderCommand(kind=CommandKind.SELECT_VIEW, view_id="top"))
    assert fake_backend[0].visible["wall"] is False, "patch visibility reset between commands"
    assert fake_backend[0].preset == "top"


@pytest.mark.parametrize("engine", VISUAL)
def test_the_evidence_sequence_is_monotonic(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    s.opening_evidence()
    seqs = [s.execute(RenderCommand(kind=CommandKind.SELECT_VIEW, view_id=v)).seq
            for v in ("front", "top", "left")]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3


@pytest.mark.parametrize("engine", VISUAL)
def test_close_is_idempotent(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    s.close(); s.close(); s.close()
    assert fake_backend[0].closed == 1


@pytest.mark.parametrize("engine", VISUAL)
def test_commands_after_close_fail(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    s.close()
    with pytest.raises(ReviewRenderError, match="closed"):
        s.execute(RenderCommand(kind=CommandKind.SELECT_VIEW, view_id="top"))


@pytest.mark.parametrize("engine", VISUAL)
def test_a_backend_that_fails_to_close_does_not_mask_the_exit(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)

    def _boom(): raise RuntimeError("driver hung")
    fake_backend[0].close = _boom
    s.close()          # must not raise


def test_renderer_and_evidence_failures_are_not_provider_downtime():
    from meshpipeline.errors import FailureClass, classify_api_failure

    for m in ("reviewer_evidence_missing", "reviewer_render_unavailable"):
        assert classify_api_failure(m) is FailureClass.REVIEW_EVIDENCE_MISSING
        assert classify_api_failure(m) is not FailureClass.PROVIDER_DOWN


# build_backend itself, unstubbed
# The fixture above replaces `build_backend` so the translation layer can be tested without a
# GPU. That leaves the function itself unexercised - a mutation swapping the surface and volume
# keys inside it survived the whole suite. These drive the real function, faking only the native
# class it constructs.
@pytest.fixture
def stub_backend_module(monkeypatch):
    import sys
    import types

    made = {}

    class _BE:
        def __init__(self, **kw):
            made.update(kw)
            self.kw = kw
        def patch_names(self): return list(PATCHES)

    mod = types.ModuleType("meshpipeline.sandbox.backend")
    mod.MeshRenderBackend = _BE
    monkeypatch.setitem(sys.modules, "meshpipeline.sandbox.backend", mod)
    return made


def test_build_backend_gives_the_backend_the_surface_as_its_surface(stub_backend_module,
                                                                    tmp_path):
    from meshpipeline.sandbox.render_adapter import build_backend

    arts = _artifacts(tmp_path)
    _, inputs, _ = build_backend(_context(tmp_path), arts)
    assert inputs.surface is arts["mesh_paths.surface"]
    assert inputs.surface.artifact_key == "mesh_paths.surface"
    assert inputs.surface.fmt is ArtifactFormat.GMSH_MSH
    assert inputs.volume is arts["mesh_paths.volume"]
    assert inputs.volume.artifact_key == "mesh_paths.volume"
    assert stub_backend_module["inputs"].surface is arts["mesh_paths.surface"]


def test_build_backend_passes_no_manifest_to_the_backend(stub_backend_module, tmp_path):
    from meshpipeline.sandbox.render_adapter import build_backend

    build_backend(_context(tmp_path), _artifacts(tmp_path))
    assert "manifest" not in stub_backend_module
    meta = stub_backend_module["metadata"]
    assert meta.mesh_units == "m" and dict(meta.patch_roles) == {"wall": "wall"}
    for v in vars(meta).values():
        assert "etc" not in str(v), "a manifest path reached the backend through metadata"


def test_build_backend_tolerates_an_absent_volume(stub_backend_module, tmp_path):
    from meshpipeline.sandbox.render_adapter import build_backend

    _, inputs, _ = build_backend(_context(tmp_path), _artifacts(tmp_path, volume=False))
    assert inputs.volume is None and inputs.has_volume is False
    assert inputs.surface is not None


def test_build_backend_refuses_when_the_surface_was_not_resolved(stub_backend_module, tmp_path):
    from meshpipeline.sandbox.render_adapter import build_backend

    with pytest.raises(ReviewRenderError, match="mesh_paths.surface"):
        build_backend(_context(tmp_path), {})


def test_build_backend_validates_regions_from_the_manifest(stub_backend_module, tmp_path):
    from meshpipeline.sandbox.render_adapter import build_backend

    _, _, regions = build_backend(_context(tmp_path), _artifacts(tmp_path))
    assert set(regions) == {"midspan", "nearwall"}
    assert stub_backend_module["regions"] == regions


def test_build_backend_uses_the_sandbox_output_directory(stub_backend_module, tmp_path):
    from meshpipeline.sandbox.render_adapter import build_backend

    build_backend(_context(tmp_path), _artifacts(tmp_path))
    assert str(stub_backend_module["save_dir"]) == str(tmp_path)


# scene context: a LIVE snapshot, per engine
def _facts(be):
    return {
        "has_geometry": bool(be.patches),
        "bounds": {"xmin": 0.0, "xmax": 100.0, "ymin": -5.0, "ymax": 5.0,
                   "zmin": 0.0, "zmax": 20.0},
        "mesh_units": "m",
        "pan_step_mm": be.pan_step,
        "zoom_step": be.zoom_step,
        "patch_legend": [(n, "#ff0000") for n in be.patches],
    }


@pytest.fixture
def scene_backend(fake_backend):
    FakeBackend.scene_facts = lambda self: _facts(self)
    yield fake_backend
    del FakeBackend.scene_facts


@pytest.mark.parametrize("engine", VISUAL)
def test_the_session_exposes_a_typed_scene_context(engine, scene_backend, tmp_path):
    from meshpipeline.contracts.review_evidence import SceneContext

    sc = _open(engine, tmp_path).scene_context()
    assert isinstance(sc, SceneContext)
    assert sc.has_geometry is True and sc.mesh_units == "m"
    assert sc.bounds.xmax == 100.0
    assert [e.patch_id for e in sc.patch_legend] == PATCHES


@pytest.mark.parametrize("engine", VISUAL)
def test_configuration_updates_the_next_scene_snapshot(engine, scene_backend, tmp_path):
    s = _open(engine, tmp_path)
    assert s.scene_context().pan_step_mm == 10.0
    s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION, pan_step_mm=42.0, zoom_step=3.0))
    after = s.scene_context()
    assert after.pan_step_mm == 42.0 and after.zoom_step == 3.0


@pytest.mark.parametrize("engine", VISUAL)
def test_recalibration_updates_the_next_scene_snapshot(engine, scene_backend, tmp_path):
    s = _open(engine, tmp_path)
    s.execute(RenderCommand(kind=CommandKind.GO_TO_COORDINATES,
                            coordinates=(1.0, 2.0, 3.0), span=500.0))
    assert s.scene_context().pan_step_mm == 50.0, "the snapshot is stale after recalibration"


@pytest.mark.parametrize("engine", VISUAL)
def test_the_snapshot_is_rebuilt_not_cached(engine, scene_backend, tmp_path):
    s = _open(engine, tmp_path)
    first = s.scene_context()
    scene_backend[0].pan_step = 77.0
    assert s.scene_context().pan_step_mm == 77.0 and first.pan_step_mm == 10.0


@pytest.mark.parametrize("engine", VISUAL)
def test_the_scene_snapshot_reproduces_todays_prompt_facts(engine, scene_backend, tmp_path):
    from meshpipeline.agents.reviewer.scene_text import navigation_context, patch_colour_legend

    sc = _open(engine, tmp_path).scene_context()
    assert navigation_context(sc)["bbox_mm"]["xmax"] == 100.0
    assert patch_colour_legend(sc) == "inlet=#ff0000, outlet=#ff0000, wall=#ff0000"


@pytest.mark.parametrize("engine", VISUAL)
def test_scene_context_after_close_is_refused(engine, scene_backend, tmp_path):
    s = _open(engine, tmp_path)
    s.close()
    with pytest.raises(ReviewRenderError, match="closed"):
        s.scene_context()


def test_geometry_presence_is_not_taken_from_the_target_count(scene_backend, tmp_path):
    s = _open("snappy", tmp_path)
    scene_backend[0].patches = []                       # nothing loaded at all
    assert s.scene_context().has_geometry is False

    scene_backend[0].patches = list(PATCHES)
    assert s.scene_context().has_geometry is True


@pytest.mark.parametrize("engine", VISUAL)
def test_the_scene_legend_labels_are_patch_names_not_reprs(engine, scene_backend, tmp_path):
    sc = _open(engine, tmp_path).scene_context()
    assert [e.label for e in sc.patch_legend] == PATCHES
    for e in sc.patch_legend:
        assert "object at 0x" not in e.label and not e.label.startswith("<")


# evidence provenance: the image must be the one the operation made
# A rendered evidence item must contain the image produced by the operation it claims to
# represent. Both defects below shipped in cbcafc1 and passed 39/39, because the harness compared
# which methods were CALLED rather than what came back.
def _img(evidence):
    return Path(evidence.image_ref).read_bytes()


@pytest.mark.parametrize("engine", VISUAL)
def test_zoom_to_region_returns_the_crop_the_backend_made(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.APPLY_CLIP, coordinates=(0.5, 0.5, 4.0)))
    assert r.image_ref, "the crop produced no evidence"
    assert _img(r) == PNG_CROP, "the crop was replaced by another operation's image"
    assert _img(r) != PNG, "a generic full view was returned as the magnified crop"


@pytest.mark.parametrize("engine", VISUAL)
def test_zoom_to_region_takes_no_generic_screenshot(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    before = fake_backend[0].generic_shots
    s.execute(RenderCommand(kind=CommandKind.APPLY_CLIP, coordinates=(0.5, 0.5, 4.0)))
    assert fake_backend[0].generic_shots == before, "a generic screenshot followed the crop"


@pytest.mark.parametrize("engine", VISUAL)
def test_inspect_region_returns_the_slice_its_operation_made(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET, target_id="region:midspan"))
    assert _img(r) == PNG_SLICE and _img(r) != PNG


@pytest.mark.parametrize("engine", VISUAL)
def test_mutating_commands_do_return_the_generic_view(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.SELECT_VIEW, view_id="front"))
    assert _img(r) == PNG


def test_every_rendering_command_has_a_pinned_image_source():
    import ast

    import meshpipeline.sandbox.render_inputs as _ri
    from meshpipeline.contracts.review_evidence import RENDERING_COMMANDS
    from meshpipeline.sandbox.render_adapter import IMAGE_SOURCE, OWN_IMAGE, THEN_SHOT

    assert set(IMAGE_SOURCE) == set(RENDERING_COMMANDS), "an operation has no declared source"

    src = (Path(inspect.getsourcefile(_ri)).parent / "backend.py").read_text()
    cls = next(n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.ClassDef) and n.name == "MeshRenderBackend")
    # An operation produces an image when it reaches a capture call that RETURNS encoded image
    # data. Since that is `SessionCapture.save_and_encode` (behind the backend's
    # `_save_and_encode` seam) and `SessionCapture.screenshot`; `png_bytes` and `assemble_video`
    # are not image-returning, which is why `close` is not a producer. The reach is transitive, so
    # routing a command through a new private helper cannot hide it from this check.
    _RETURNS_IMAGE = ("_capture.save_and_encode", "_capture.screenshot")
    methods = {fn.name: fn for fn in cls.body if isinstance(fn, ast.FunctionDef)}
    calls = {name: {ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
             for name, fn in methods.items()}
    reaches = {n for n, cs in calls.items() if any(m in c for c in cs for m in _RETURNS_IMAGE)}
    for _ in range(len(methods)):   # transitive closure over `self.<method>()` edges
        grown = reaches | {n for n, cs in calls.items()
                           if any(f"self.{r}" in cs for r in reaches)}
        if grown == reaches:
            break
        reaches = grown
    # Report the dispatchable operations, not the private seams they share.
    produces = {n for n in reaches if not n.startswith("_")}
    assert produces == {"take_screenshot", "inspect_region", "zoom_to_region"}, (
        f"the image-producing operations changed: {sorted(produces)}")
    assert IMAGE_SOURCE[CommandKind.APPLY_CLIP] is OWN_IMAGE
    assert IMAGE_SOURCE[CommandKind.INSPECT_TARGET] is OWN_IMAGE
    assert IMAGE_SOURCE[CommandKind.SELECT_VIEW] is THEN_SHOT


# go_to_coordinates forwards all six inputs
@pytest.mark.parametrize("engine", VISUAL)
def test_go_to_coordinates_forwards_every_argument(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    s.execute(RenderCommand(kind=CommandKind.GO_TO_COORDINATES, coordinates=(1.0, 2.0, 3.0),
                            span=10.0, view_id="top", entity_id="wall"))
    fake = fake_backend[0]
    assert ("go_to_coordinates", 1.0, 2.0, 3.0, 10.0) in fake.calls
    assert fake.kwargs[-1] == {"preset": "top", "patch_name": "wall"}


@pytest.mark.parametrize("engine", VISUAL)
def test_go_to_coordinates_actually_applies_preset_and_isolation(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    s.execute(RenderCommand(kind=CommandKind.GO_TO_COORDINATES, coordinates=(1.0, 2.0, 3.0),
                            span=10.0, view_id="top", entity_id="wall"))
    fake = fake_backend[0]
    assert fake.preset == "top", "the requested preset was never applied"
    assert fake.isolated == "wall", "the requested patch was never isolated"
    assert fake.visible == {"inlet": False, "outlet": False, "wall": True}


@pytest.mark.parametrize("engine", VISUAL)
def test_absent_preset_and_patch_are_forwarded_as_none(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    s.execute(RenderCommand(kind=CommandKind.GO_TO_COORDINATES,
                            coordinates=(1.0, 2.0, 3.0), span=10.0))
    assert fake_backend[0].kwargs[-1] == {"preset": None, "patch_name": None}
    assert fake_backend[0].isolated is None


@pytest.mark.parametrize("engine", VISUAL)
def test_go_to_coordinates_refuses_an_unknown_patch_before_the_backend(engine, fake_backend,
                                                                       tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.GO_TO_COORDINATES, coordinates=(1.0, 2.0, 3.0),
                                span=10.0, entity_id="nonexistent"))
    assert r.image_ref == "" and r.covers_target == ""
    assert not any(c[0] == "go_to_coordinates" for c in fake_backend[0].calls)


@pytest.mark.parametrize("engine", VISUAL)
def test_go_to_coordinates_refuses_an_unknown_preset(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.GO_TO_COORDINATES, coordinates=(1.0, 2.0, 3.0),
                                span=10.0, view_id="sideways"))
    assert r.image_ref == ""
    assert not any(c[0] == "go_to_coordinates" for c in fake_backend[0].calls)


# coverage cannot be earned with a substituted image
@pytest.mark.parametrize("engine", VISUAL)
def test_coverage_cannot_be_credited_from_a_substituted_image(engine, fake_backend, tmp_path):
    s = _open(engine, tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.INSPECT_TARGET, target_id="region:midspan"))
    assert r.covers_target == "region:midspan"
    assert _img(r) == PNG_SLICE, "coverage was credited on a foreign image"
