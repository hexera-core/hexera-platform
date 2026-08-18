# Responsibility: Verify a Gmsh review target is a physical group, isolated, and refused when the name is ambiguous.
from __future__ import annotations

from pathlib import Path

import pytest

from meshpipeline.contracts.review_evidence import (
    CommandKind,
    RenderCommand,
    SessionUpdateResult,
    TargetKind,
)
from meshpipeline.engines.gmsh.review_renderer import GmshReviewSession
from meshpipeline.engines.gmsh.review_targets import (
    discover_group_targets,
    resolve_target,
)

# A non-symmetric FEA-like group set: two "fixed" groups distinct by dimension, an unnamed group,
# a load face, an empty (unrenderable) group. Non-round member tags.
RAW = [
    (2, 5, "load_face", (11, 13)),
    (2, 3, "fixed", (7,)),
    (3, 3, "fixed", (21,)),          # same NAME as the 2D fixed, distinct (dim, tag)
    (2, 9, "", (17,)),               # unnamed
    (2, 4, "contact", ()),           # no members -> not renderable
]

PNG = {"open": b"\x89PNG\r\n\x1a\nOPEN" + b"\x00" * 40,
       "shot": b"\x89PNG\r\n\x1a\nSHOT" + b"\x00" * 40,
       "crop": b"\x89PNG\r\n\x1a\nCROP" + b"\x00" * 40}


class GmshFakeBackend:

    def __init__(self, save_dir):
        self.visible: dict[str, bool] = {}
        self.calls: list = []
        self.pan_step, self.zoom_step = 7.5, 1.5
        self.closed = 0
        self._dir = Path(save_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self.last_shot_path = None
        self._n = 0

    def physical_groups(self): return list(RAW)

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
    def go_to_coordinates(self, x, y, z, span, preset=None): self.calls.append(("goto", x, y, z, span, preset))
    def toggle_patch(self, name, visible): self.visible[name] = visible; self.calls.append(("toggle", name, visible))
    def set_navigation_defaults(self, pan=None, zoom=None):
        if pan and pan > 0: self.pan_step = float(pan)
        if zoom and zoom > 0: self.zoom_step = float(zoom)
        return {"pan_step_mm": self.pan_step, "zoom_step": self.zoom_step}
    def close(self): self.closed += 1


def _session(tmp_path):
    be = GmshFakeBackend(tmp_path)
    targets = discover_group_targets(be.physical_groups())
    return GmshReviewSession(be, targets, required=True, purpose="check group placement"), be


def _img(ev):
    return Path(ev.image_ref).read_bytes() if ev.image_ref else None


# target model
def test_target_ids_are_physical_and_order_independent():
    a = [t.target_id for t in discover_group_targets(RAW)]
    b = [t.target_id for t in discover_group_targets(list(reversed(RAW)))]
    assert a == b, "target order/identity depends on enumeration order"
    assert a == ["group:2:3", "group:2:4", "group:2:5", "group:2:9", "group:3:3"]


def test_same_named_groups_stay_distinct():
    t = discover_group_targets(RAW)
    fixed = [x for x in t if x.name == "fixed"]
    assert {x.target_id for x in fixed} == {"group:2:3", "group:3:3"}


def test_kind_is_group_never_patch():
    for t in discover_group_targets(RAW):
        it = t.to_inspection_target(required=True, purpose="p")
        assert it.kind is TargetKind.GROUP
        assert it.target_id.startswith("group:") and "patch:" not in it.target_id


def test_unnamed_and_empty_groups_are_handled():
    t = {x.target_id: x for x in discover_group_targets(RAW)}
    assert t["group:2:9"].name == "" and t["group:2:9"].renderable
    assert not t["group:2:4"].renderable          # no members


def test_a_unique_name_resolves_but_a_shared_name_is_ambiguous():
    t = discover_group_targets(RAW)
    assert resolve_target(t, "load_face")[0].target_id == "group:2:5"
    tgt, err = resolve_target(t, "fixed")
    assert tgt is None and "distinct physical groups" in err
    assert resolve_target(t, "group:2:3")[0].name == "fixed"     # id disambiguates
    assert resolve_target(t, "nope")[0] is None


# session capabilities + coverage
def test_capabilities_expose_group_targets(tmp_path):
    s, _ = _session(tmp_path)
    caps = s.capabilities()
    assert all(t.kind is TargetKind.GROUP for t in caps.targets)
    assert CommandKind.TOGGLE_ENTITY in caps.operations


def test_isolating_a_group_hides_the_others_and_covers_a_group_token(tmp_path):
    s, be = _session(tmp_path)
    ev = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="load_face",
                                 visible=False))
    assert ev.covers_target == "group:2:5", "coverage must be a GROUP token, never patch:"
    assert "patch:" not in ev.covers_target
    assert be.visible["load_face"] is False and be.visible["fixed"] is True
    assert ev.image_ref and _img(ev) == PNG["shot"]


def test_unknown_group_renders_nothing_and_covers_nothing(tmp_path):
    s, be = _session(tmp_path)
    ev = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="ghost", visible=False))
    assert ev.image_ref == "" and ev.covers_target == ""
    assert not any(c[0] == "toggle" for c in be.calls)


def test_ambiguous_group_name_is_refused_not_first_matched(tmp_path):
    s, be = _session(tmp_path)
    ev = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="fixed", visible=False))
    assert ev.image_ref == "" and "distinct physical groups" in ev.diagnostics[0]
    assert not any(c[0] == "toggle" for c in be.calls)


def test_a_group_with_no_members_cannot_be_inspected(tmp_path):
    s, _ = _session(tmp_path)
    ev = s.execute(RenderCommand(kind=CommandKind.TOGGLE_ENTITY, entity_id="group:2:4",
                                 visible=False))
    assert ev.image_ref == "" and "no renderable members" in ev.diagnostics[0]


# operation-owned images
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


def test_commands_after_close_fail(tmp_path):
    s, be = _session(tmp_path)
    s.close()
    assert be.closed == 1
    from meshpipeline.contracts.review_evidence import ReviewRenderError
    with pytest.raises(ReviewRenderError, match="closed"):
        s.execute(RenderCommand(kind=CommandKind.SELECT_VIEW, view_id="top"))


# the adapter opens a REAL session (not a placeholder)
def test_open_returns_a_real_gmsh_session_not_a_placeholder(monkeypatch, tmp_path):
    from meshpipeline.contracts.review_evidence import (
        ArtifactFormat,
        RenderContext,
        ResolvedArtifact,
    )
    from meshpipeline.engines.gmsh.review_renderer import GmshReviewRenderer

    be = GmshFakeBackend(tmp_path)
    import meshpipeline.sandbox.render_adapter as RA

    def _build(context, artifacts, **kw):
        from meshpipeline.sandbox.render_inputs import ResolvedRenderInputs
        return be, ResolvedRenderInputs(surface=artifacts["mesh_paths.surface"], volume=None), {}
    monkeypatch.setattr(RA, "build_backend", _build)
    import meshpipeline.engines.gmsh.review_renderer as GR
    monkeypatch.setattr(GR, "build_backend", _build, raising=False)

    ctx = RenderContext(workspace=str(tmp_path), save_dir=str(tmp_path),
                        manifest={"mesh_paths": {"surface": str(tmp_path / "m.msh")}})
    arts = {"mesh_paths.surface": ResolvedArtifact(
        "mesh_paths.surface", tmp_path / "m.msh", ArtifactFormat.GMSH_MSH)}

    session = GmshReviewRenderer().open(ctx, arts)
    assert type(session).__name__ == "GmshReviewSession"
    caps = session.capabilities()
    assert caps.targets and all(t.kind is TargetKind.GROUP for t in caps.targets)
