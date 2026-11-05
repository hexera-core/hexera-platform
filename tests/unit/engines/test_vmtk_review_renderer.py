# Responsibility: Verify VMTK review targets are vascular - wall, openings, branches - and refused when ambiguous.
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.contracts.review_evidence import (
    CommandKind,
    RenderCommand,
    ReviewRenderError,
    SessionUpdateResult,
    TargetKind,
)
from meshpipeline.engines.vmtk.review_renderer import VmtkReviewSession
from meshpipeline.engines.vmtk.review_targets import (
    VmtkLayerTarget,
    VmtkOpeningTarget,
    audit_cross_artifacts,
    discover_branch_targets,
    discover_surface_targets,
    resolve_target,
)

# An UNSORTED patch set with exactly one wall (vmtk's own convention) and three caps.
PATCHES = ["outlet_b", "wall", "inlet", "outlet_a"]

# Centerlines in a non-sorted, non-round layout: a low branch, a degenerate single-point "branch",
# and a high branch. Sorted by endpoint geometry, ids must not follow this storage order.
PL_LO = ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 2.0, 0.0))
PL_DEG = ((5.0, 5.0, 5.0),)                       # one point -> not a navigable passage
PL_HI = ((10.0, 0.0, 0.0), (11.0, 0.0, 0.0), (12.0, 0.0, 0.0))
POLYLINES = [PL_HI, PL_DEG, PL_LO]

PNG = {"open": b"\x89PNG\r\n\x1a\nOPEN" + b"\x00" * 40,
       "shot": b"\x89PNG\r\n\x1a\nSHOT" + b"\x00" * 40,
       "crop": b"\x89PNG\r\n\x1a\nCROP" + b"\x00" * 40}


class VmtkFakeBackend:

    def __init__(self, save_dir):
        self.visible: dict[str, bool] = {}
        self.calls: list = []
        self.pan_step, self.zoom_step = 7.5, 1.5
        self.closed = 0
        self._dir = Path(save_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.last_shot_path = None
        self._n = 0

    def patch_names(self): return list(PATCHES)

    def _emit(self, key):
        self._n += 1
        p = self._dir / f"{key}{self._n:03d}.png"
        p.write_bytes(PNG.get(key, PNG["shot"]))
        self.last_shot_path = p
        return "b64"

    def take_screenshot(self): self.calls.append(("shot",)); return self._emit("shot")
    def zoom_to_region(self, sx, sy, mag): self.calls.append(("crop", sx, sy, mag)); return self._emit("crop")
    def set_camera_preset(self, p): self.calls.append(("preset", p))
    def move_camera(self, d, a=None): self.calls.append(("move", d, a))
    def rotate_camera(self, a, d): self.calls.append(("rot", a, d))
    def zoom(self, f=None): self.calls.append(("zoom", f))
    def reset_view(self): self.calls.append(("reset",))
    def go_to_coordinates(self, x, y, z, span, preset=None):
        self.calls.append(("goto", x, y, z, span, preset)); return self._emit("shot")
    def toggle_patch(self, name, visible): self.visible[name] = visible; self.calls.append(("toggle", name, visible))
    def set_navigation_defaults(self, pan=None, zoom=None):
        if pan and pan > 0: self.pan_step = float(pan)
        if zoom and zoom > 0: self.zoom_step = float(zoom)
        return {"pan_step_mm": self.pan_step, "zoom_step": self.zoom_step}
    def close(self): self.closed += 1


def _session(tmp_path, *, submitted_loops=3):
    be = VmtkFakeBackend(tmp_path)
    openings, layers = discover_surface_targets(be.patch_names())
    branches = discover_branch_targets(POLYLINES)
    audit = audit_cross_artifacts(openings, submitted_loops, branches)
    s = VmtkReviewSession(be, (*openings, *layers, *branches), audit,
                          required=False, purpose="check the lumen anatomy survived")
    return s, be


def _img(ev):
    return Path(ev.image_ref).read_bytes() if ev.image_ref else None


# target model
def test_wall_is_the_layer_region_and_the_rest_are_openings():
    openings, layers = discover_surface_targets(PATCHES)
    assert [o.target_id for o in openings] == ["opening:inlet", "opening:outlet_a",
                                               "opening:outlet_b"]
    assert [layer.target_id for layer in layers] == ["layer_region:wall"]


def test_surface_split_is_order_independent():
    a = discover_surface_targets(PATCHES)
    b = discover_surface_targets(list(reversed(PATCHES)))
    assert [t.target_id for t in a[0]] == [t.target_id for t in b[0]]
    assert [t.target_id for t in a[1]] == [t.target_id for t in b[1]]


def test_a_lumen_with_no_wall_yields_no_layer_region():
    openings, layers = discover_surface_targets(["inlet", "outlet"])
    assert layers == () and len(openings) == 2


def test_branch_ids_come_from_geometry_not_storage_order():
    a = [b.target_id for b in discover_branch_targets(POLYLINES)]
    b = [b.target_id for b in discover_branch_targets(list(reversed(POLYLINES)))]
    assert a == b == ["branch:0", "branch:1", "branch:2"]


def test_a_degenerate_branch_is_retained_but_not_renderable():
    br = {b.target_id: b for b in discover_branch_targets(POLYLINES)}
    # PL_DEG sorts between PL_LO and PL_HI by its (5,5,5) endpoint -> branch:1
    assert br["branch:1"].n_points == 1 and not br["branch:1"].renderable
    assert br["branch:0"].renderable and br["branch:2"].renderable


def test_kinds_are_vascular_never_patch():
    openings, layers = discover_surface_targets(PATCHES)
    branches = discover_branch_targets(POLYLINES)
    kinds = {TargetKind.OPENING, TargetKind.BRANCH, TargetKind.LAYER_REGION}
    for t in (*openings, *layers, *branches):
        it = t.to_inspection_target(required=False, purpose="p")
        assert it.kind in kinds
        assert not it.target_id.startswith("patch:")


def test_a_unique_name_resolves_but_a_shared_name_is_ambiguous():
    openings, layers = discover_surface_targets(PATCHES)
    branches = discover_branch_targets(POLYLINES)
    targets = (*openings, *layers, *branches)
    assert resolve_target(targets, "inlet")[0].target_id == "opening:inlet"
    assert resolve_target(targets, "wall")[0].target_id == "layer_region:wall"
    assert resolve_target(targets, "branch:2")[0].target_id == "branch:2"
    assert resolve_target(targets, "ghost")[0] is None
    # a name shared by two targets must NOT resolve to the first match
    dup = (VmtkOpeningTarget("opening:x", "x", "x", True),
           VmtkLayerTarget("layer_region:x", "x", "x", True))
    tgt, err = resolve_target(dup, "x")
    assert tgt is None and "distinct targets" in err


# cross-artifact audit
def test_matching_caps_and_open_profiles_raise_no_mismatch():
    openings, _ = discover_surface_targets(PATCHES)      # 3 caps
    au = audit_cross_artifacts(openings, 3, ())
    assert au.mismatches == () and au.delivered_openings == 3


def test_a_lost_opening_is_flagged_against_the_submitted_lumen():
    openings, _ = discover_surface_targets(PATCHES)      # 3 caps delivered
    au = audit_cross_artifacts(openings, 4, ())          # lumen had 4 open profiles
    assert au.mismatches and "lost" in au.mismatches[0]


def test_an_absent_lumen_is_unknown_not_a_mismatch():
    openings, _ = discover_surface_targets(PATCHES)
    au = audit_cross_artifacts(openings, -1, ())
    assert au.mismatches == () and au.submitted_open_loops == -1


# session capabilities + coverage
def test_capabilities_expose_vascular_targets(tmp_path):
    s, _ = _session(tmp_path)
    caps = s.capabilities()
    assert {t.kind for t in caps.targets} == {TargetKind.OPENING, TargetKind.BRANCH,
                                              TargetKind.LAYER_REGION}
    assert CommandKind.TOGGLE_ENTITY in caps.operations


def test_isolating_an_opening_hides_the_others_and_covers_an_opening_token(tmp_path):
    s, be = _session(tmp_path)
    ev = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="inlet", visible=False))
    assert ev.covers_target == "opening:inlet", "coverage must be a vmtk token, never patch:"
    assert "patch:" not in ev.covers_target
    assert be.visible["inlet"] is False and be.visible["outlet_a"] is True and be.visible["wall"] is True
    assert ev.image_ref and _img(ev) == PNG["shot"]


def test_isolating_the_wall_covers_a_layer_region_token(tmp_path):
    s, be = _session(tmp_path)
    ev = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="wall", visible=True))
    assert ev.covers_target == "layer_region:wall" and "patch:" not in ev.covers_target
    assert be.visible["wall"] is True


def test_inspecting_a_branch_navigates_the_camera_and_covers_a_branch_token(tmp_path):
    s, be = _session(tmp_path)
    ev = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="branch:0", visible=True))
    assert ev.covers_target == "branch:0" and "patch:" not in ev.covers_target
    gotos = [c for c in be.calls if c[0] == "goto"]
    assert len(gotos) == 1, "a branch is inspected by navigating to it, not by toggling a patch"
    assert not any(c[0] == "toggle" for c in be.calls)
    assert ev.image_ref and _img(ev) == PNG["shot"]


def test_unknown_target_renders_nothing_and_covers_nothing(tmp_path):
    s, be = _session(tmp_path)
    ev = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="ghost", visible=False))
    assert ev.image_ref == "" and ev.covers_target == ""
    assert not any(c[0] in ("toggle", "goto") for c in be.calls)


def test_a_degenerate_branch_cannot_be_inspected(tmp_path):
    s, be = _session(tmp_path)
    ev = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="branch:1", visible=True))
    assert ev.image_ref == "" and "no renderable geometry" in ev.diagnostics[0]
    assert not any(c[0] == "goto" for c in be.calls)


# operation-owned images + lifecycle
def test_the_crop_returns_its_own_image_not_a_screenshot(tmp_path):
    s, _ = _session(tmp_path)
    crop = s.execute(RenderCommand(kind=CommandKind.APPLY_CLIP, coordinates=(0.2, 0.3, 0.0),
                                   span=8.0))
    preset = s.execute(RenderCommand(kind=CommandKind.SELECT_VIEW, view_id="front"))
    assert _img(crop) == PNG["crop"] and _img(preset) == PNG["shot"]
    assert _img(crop) != _img(preset)


def test_configuration_is_text_only(tmp_path):
    s, _ = _session(tmp_path)
    r = s.execute(RenderCommand(kind=CommandKind.CONFIGURE_SESSION, pan_step_mm=25.0))
    assert isinstance(r, SessionUpdateResult) and "pan_step=25 mm" in r.message


def test_opening_evidence_is_a_real_image(tmp_path):
    s, _ = _session(tmp_path)
    ev = s.opening_evidence()
    assert len(ev) == 1 and ev[0].image_ref and ev[0].covers_target == ""


def test_the_session_carries_its_cross_artifact_audit(tmp_path):
    s, _ = _session(tmp_path, submitted_loops=4)         # 3 caps vs 4 profiles
    assert s.scene_audit().mismatches, "a lost opening must be visible on the session's audit"


def test_commands_after_close_fail(tmp_path):
    s, be = _session(tmp_path)
    s.close()
    assert be.closed == 1
    with pytest.raises(ReviewRenderError, match="closed"):
        s.execute(RenderCommand(kind=CommandKind.SELECT_VIEW, view_id="top"))


# the adapter opens a REAL session (not a placeholder)
def test_open_returns_a_real_vmtk_session_not_a_placeholder(monkeypatch, tmp_path):
    from meshpipeline.contracts.review_evidence import (
        ArtifactFormat,
        RenderContext,
        ResolvedArtifact,
    )
    from meshpipeline.engines.vmtk.review_renderer import VmtkReviewRenderer

    be = VmtkFakeBackend(tmp_path)

    def _build(context, artifacts, **kw):
        from meshpipeline.sandbox.render_inputs import ResolvedRenderInputs
        return be, ResolvedRenderInputs(surface=artifacts["mesh_paths.surface"], volume=None), {}

    import meshpipeline.engines.vmtk.review_renderer as VR
    import meshpipeline.sandbox.render_adapter as RA
    monkeypatch.setattr(RA, "build_backend", _build)
    # centerlines present -> branches discovered; loaders are patched to avoid needing real files
    monkeypatch.setattr("meshpipeline.sandbox.polyline_source.load_polylines",
                        lambda p: POLYLINES, raising=True)
    monkeypatch.setattr("meshpipeline.sandbox.polyline_source.count_boundary_loops",
                        lambda p: 3, raising=True)

    surf = tmp_path / "mesh.msh"
    cl = tmp_path / "centerlines.vtp"
    lum = tmp_path / "lumen.vtp"
    for f in (surf, cl, lum):
        f.write_bytes(b"x")
    ctx = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path),
                        manifest={"mesh_paths": {"surface": str(surf)}})
    arts = {
        "mesh_paths.surface": ResolvedArtifact("mesh_paths.surface", surf, ArtifactFormat.GMSH_MSH),
        "mesh_paths.centerlines": ResolvedArtifact("mesh_paths.centerlines", cl,
                                                   ArtifactFormat.VTK_VTP),
        "mesh_paths.lumen": ResolvedArtifact("mesh_paths.lumen", lum, ArtifactFormat.VTK_VTP),
    }
    session = VmtkReviewRenderer().open(ctx, arts)
    assert type(session).__name__ == "VmtkReviewSession"
    caps = session.capabilities()
    kinds = {t.kind for t in caps.targets}
    assert TargetKind.OPENING in kinds and TargetKind.BRANCH in kinds
    assert session.scene_audit().branch_count == 3   # all three polylines become branch targets
    assert VR  # module imported
